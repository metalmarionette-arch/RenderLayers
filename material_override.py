# material_override.py

import bpy
from bpy.app.handlers import persistent
from bpy.props import PointerProperty, CollectionProperty
from bpy.types import PropertyGroup

# ───────────────────────────────
# 直前にアクティブだったビュー・レイヤー名を入れておくグローバル変数
# （初期値は空文字で可）
_prev_active_viewlayer_name: str = ""
# ───────────────────────────────


class MaterialBackupItem(PropertyGroup):
    material: PointerProperty(
        name="Backup Material",
        type=bpy.types.Material,
        description="元のマテリアル参照"
    )
# --------------------------------------------------
# 内部キー
# --------------------------------------------------
def _backup_key(obj):
    return "_vlm_mat_backup"

def _override_key(vl_name, col_name):
    return f"_vlm_mat_override_{vl_name}_{col_name}"

# --------------------------------------------------
# マテリアル バックアップ／復元
# --------------------------------------------------
def backup_all_materials():
    """
    まだバックアップが無いオブジェクトだけ pointer で保存する。
    PointerProperty はマテリアル名を変更しても追随するため
    基本的に取り直しは不要（必要なら［マテリアルバックアップ］で上書き）。
    """
    for obj in bpy.data.objects:
        if obj.type != 'MESH':
            continue

        # すでにバックアップ済みならスキップ
        if getattr(obj, "backup_materials", None) and obj.backup_materials:
            continue

        for slot in obj.material_slots:
            item = obj.backup_materials.add()
            item.material = slot.material

        # 旧ロジック互換フラグ
        obj[_backup_key(obj)] = ""

# _viewlayer_changed_handler 関数をこの内容に差し替えてください

# material_override.py の _viewlayer_changed_handler 関数をこの内容に差し替えてください

@persistent
def _viewlayer_changed_handler(depsgraph):
    """
    depsgraph_update_post ハンドラ。
    標準UIでビューレイヤーが変わった時だけ、各種オーバーライドを適用する。
    ※ レンダリング中は絶対に何もしない（Eeveeボリューム等のクラッシュ回避）
    """
    global _prev_active_viewlayer_name

    # 1) コンテキストが未整備なら何もしない
    try:
        context = bpy.context
        current_vl = context.window.view_layer
        current_sc = context.scene
    except (AttributeError, RuntimeError):
        return
    if current_vl is None or current_sc is None:
        return

    # 2) 初回同期が完了するまでは何もしない
    if not current_sc.get("vlm_settings_synced", False):
        return

    # 3) ★レンダリング中は一切処理しない（最重要）
    try:
        # Blender 3.0+ で利用可。古い版では AttributeError になるので二重ガード
        if hasattr(bpy.app, "is_job_running") and bpy.app.is_job_running("RENDER"):
            return
    except Exception:
        return

    # 4) 初回は現在名を記録するだけ（適用はしない）
    name_now = current_vl.name
    if not _prev_active_viewlayer_name:
        _prev_active_viewlayer_name = name_now
        return

    # 5) 変化がなければ何もしない
    if name_now == _prev_active_viewlayer_name:
        return

    # 6) ここまで来たら「実際に切り替わった」ので適用
    _prev_active_viewlayer_name = name_now

    from . import light_camera
    from .render_override import apply_render_override
    # アクティブVL向けの一括適用（マテリアル／ライト／レンダー設定）
    apply_active_viewlayer_overrides(context)
    light_camera.apply_lights_for_viewlayer(current_vl)
    apply_render_override(current_sc, current_vl)
        
def restore_all_materials():
    """バックアップから直接 Material ポインタを復元"""
    for obj in bpy.data.objects:
        if obj.type != 'MESH' or not hasattr(obj, "backup_materials"):
            continue
        for i, item in enumerate(obj.backup_materials):
            if i < len(obj.material_slots):
                obj.material_slots[i].material = item.material


def _set_collection_material(col: bpy.types.Collection,
                             mat_name: str,
                             view_layer: bpy.types.ViewLayer):
    """コレクション内の可視 Mesh オブジェクトへ一括でマテリアルを設定"""
    mat = bpy.data.materials.get(mat_name)
    if not mat:
        return  # 見つからなければスキップ

    for obj in col.objects:
        if obj.type == 'MESH' and obj.visible_get(view_layer=view_layer):
            for slot in obj.material_slots:
                slot.material = mat

def _iter_layer_collections_recursive(layer_coll):
    """
    与えられた LayerCollection の **子孫** を再帰的に yield する
    （自身 layer_coll は含めず、children_recursive と同等の挙動）
    """
    for child in layer_coll.children:
        yield child
        yield from _iter_layer_collections_recursive(child)

# --------------------------------------------------
# ViewLayer ごとのマテリアル上書きロジック
# --------------------------------------------------
def _apply_overrides_for_viewlayer(view_layer: bpy.types.ViewLayer):
    """
    指定ビューレイヤーに対し、
    オーバーライドキーを持つコレクションのみ
    マテリアルを再設定する。
    """
    # ★ 修正: 一番上のビューレイヤーでは何もしない
    if view_layer.name == bpy.context.scene.view_layers[0].name:
        return
        
    for lc in _iter_layer_collections_recursive(view_layer.layer_collection):
        col = lc.collection
        key = _override_key(view_layer.name, col.name)
        if key in col:
            _set_collection_material(col, col[key], view_layer)

def _apply_selective_material_overrides(view_layer: bpy.types.ViewLayer):
    """
    1) 初回だけバックアップを確保（既にあればスキップ）
    2) アクティブ ViewLayer 全体をバックアップへロールバック
    3) 当該 ViewLayer のコレクション単位オーバーライドを再適用
    """
    backup_all_materials()                     # ❶ バックアップ（初回のみ）
    _restore_viewlayer_materials(view_layer)   # ❷ まず全部「元に戻す」
    _apply_overrides_for_viewlayer(view_layer) # ❸ その上でオーバーライド

def apply_active_viewlayer_overrides(context):
    """アクティブなビューレイヤーにマテリアルオーバーライドを適用"""
    _apply_selective_material_overrides(context.view_layer)

def force_create_initial_backup():
    """既存のバックアップを削除して新しくバックアップを作成"""
    for obj in bpy.data.objects:
        if obj.type == 'MESH':
            # 既存のバックアップキーを削除
            key = _backup_key(obj)
            if key in obj:
                del obj[key]
    # 新しくバックアップを作成
    backup_all_materials()

def _restore_collection_materials(collection: bpy.types.Collection,
                                  view_layer: bpy.types.ViewLayer):
    """
    指定コレクションに属する Mesh オブジェクトを
    pointer バックアップから復元する（文字列バックアップはフォールバック用）
    """
    for obj in collection.all_objects:
        if obj.type != 'MESH' or not obj.visible_get(view_layer=view_layer):
            continue

        # --- pointer があれば優先 ---
        if getattr(obj, "backup_materials", None) and obj.backup_materials:
            for i, item in enumerate(obj.backup_materials):
                if i < len(obj.material_slots):
                    obj.material_slots[i].material = item.material
            obj.update_tag()
            continue  # pointer で復元済み

        # --- 旧式（文字列）バックアップが残っている場合のみフォールバック ---
        key = _backup_key(obj)
        if key in obj:
            names = obj[key].split(",")
            for i, name in enumerate(names):
                if i < len(obj.material_slots):
                    obj.material_slots[i].material = bpy.data.materials.get(name) if name else None
            obj.update_tag()


def _restore_viewlayer_materials(view_layer: bpy.types.ViewLayer):
    """
    アクティブ ViewLayer で *表示されている* 全オブジェクトを
    バックアップ状態に戻す。
    """
    for obj in bpy.data.objects:
        if obj.type != 'MESH':
            continue
        if not obj.visible_get(view_layer=view_layer):
            continue
        # ポインタ方式
        if getattr(obj, "backup_materials", None) and obj.backup_materials:
            for i, item in enumerate(obj.backup_materials):
                if i < len(obj.material_slots):
                    obj.material_slots[i].material = item.material
            continue
        # 文字列バックアップ（旧形式）のフォールバック
        key = _backup_key(obj)
        if key in obj:
            names = obj[key].split(",")
            for i, name in enumerate(names):
                if i < len(obj.material_slots):
                    obj.material_slots[i].material = bpy.data.materials.get(name) if name else None

# --------------------------------------------------
# マテリアル上書き関連オペレーター
# --------------------------------------------------
class COLMATERIAL_OT_set_collection_override(bpy.types.Operator):
    bl_idname = "colm.set_collection_override"
    bl_label  = "コレクションにマテリアル上書き"
    bl_options = {'REGISTER', 'UNDO'}

    collection_name : bpy.props.StringProperty()
    material_name   : bpy.props.StringProperty()
    layer_name      : bpy.props.StringProperty()

    def execute(self, context):
        from . import light_camera
        
        col = bpy.data.collections[self.collection_name]
        col[_override_key(self.layer_name, self.collection_name)] = self.material_name
        apply_active_viewlayer_overrides(context)
        # ライト設定も適用
        light_camera.apply_lights_for_viewlayer(context.view_layer)
        return {'FINISHED'}

class COLMATERIAL_OT_clear_collection_override(bpy.types.Operator):
    bl_idname  = "colm.clear_collection_override"
    bl_label   = "マテリアル上書き解除"
    bl_options = {'REGISTER', 'UNDO'}

    collection_name: bpy.props.StringProperty()
    layer_name     : bpy.props.StringProperty()

    def execute(self, context):
        col         = bpy.data.collections[self.collection_name]
        view_layer  = context.scene.view_layers[self.layer_name]

        # ① この ViewLayer 用のオーバーライドキーを削除
        key = _override_key(self.layer_name, self.collection_name)
        if key in col:
            del col[key]

        # ② 該当コレクションだけを元に戻す
        _restore_collection_materials(col, view_layer)

        # ③ レイヤー全体で必要なオーバーライドを再適用
        _apply_overrides_for_viewlayer(view_layer)

        return {'FINISHED'}


# ==== 追加：今の見た目を保存（上書き） =====================================

class VLM_OT_backup_materials_current(bpy.types.Operator):
    bl_idname = "vlm.backup_materials_current"
    bl_label  = "今の見た目を保存"
    bl_description = (
        "現在の表示状態（オーバーライドを含む“今見えているマテリアル”）をそのままバックアップします。\n"
        "既存のバックアップは一度クリアしてから保存します。"
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        try:
            return context.scene is not None
        except Exception:
            return False

    def execute(self, context):
        # 既存バックアップを全クリア
        for obj in bpy.data.objects:
            if obj.type != 'MESH':
                continue
            if getattr(obj, "backup_materials", None):
                obj.backup_materials.clear()
            key = _backup_key(obj)
            if key in obj:
                try:
                    del obj[key]
                except Exception:
                    pass

        # 今の見た目をそのまま保存
        backup_all_materials()
        self.report({'INFO'}, "現在の見た目でマテリアルバックアップを更新しました")
        return {'FINISHED'}


# ==== 追加：ベース状態（オーバーライド無視）で保存 =========================

def _visible_collections_in_viewlayer(vl: bpy.types.ViewLayer):
    """この ViewLayer のレイヤーコレクション配下にぶら下がっている Collection をセットで返す"""
    cols = set()
    def rec(lc):
        cols.add(lc.collection)
        for child in lc.children:
            rec(child)
    rec(vl.layer_collection)
    return cols

def _overridden_collections_for(vl: bpy.types.ViewLayer):
    """この ViewLayer 名でオーバーライド指定のある Collection を（可視ツリー内に限って）返す"""
    vis_cols = _visible_collections_in_viewlayer(vl)
    over = set()
    for col in vis_cols:
        key = _override_key(vl.name, col.name)
        if key in col:
            over.add(col)
    return over

class VLM_OT_backup_materials_base(bpy.types.Operator):
    bl_idname = "vlm.backup_materials_base"
    bl_label  = "ベース状態を保存（オーバーライド無視）"
    bl_description = (
        "2つ目以降のビューレイヤーでも使用可。\n"
        "このレイヤーでオーバーライドが設定されたコレクションに属するオブジェクトは、"
        "既存バックアップ（=ベース）を優先して保存します（オーバーライドを無視）。\n"
        "ベースが未作成のオブジェクトは現在の状態で保存されます。"
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        try:
            return context.scene is not None and context.view_layer is not None
        except Exception:
            return False

    def execute(self, context):
        vl = context.view_layer
        vis_cols = _visible_collections_in_viewlayer(vl)
        overridden_cols = _overridden_collections_for(vl)

        # 1) まず各オブジェクトごとに「保存候補のマテリアル配列」を作っておく
        plan = {}  # obj -> [mat0, mat1, ...]
        fallback_used = False

        def obj_in_overridden(o: bpy.types.Object) -> bool:
            # 可視ツリー上のどれかのコレクションでオーバーライドされていれば対象
            for c in o.users_collection:
                if (c in vis_cols) and (c in overridden_cols):
                    return True
            return False

        for obj in bpy.data.objects:
            if obj.type != 'MESH':
                continue

            # 既存ベース（Pointerバックアップ）があるか
            has_base = bool(getattr(obj, "backup_materials", None) and obj.backup_materials)

            if obj_in_overridden(obj) and has_base:
                # オーバーライド対象だがベースを持っている ⇒ ベースを採用
                mats = [item.material for item in obj.backup_materials]
                plan[obj] = mats
            else:
                # それ以外は「今の見た目」を採用（※ベース無いオブジェクトはここに来る）
                mats = [slot.material for slot in obj.material_slots]
                plan[obj] = mats
                if obj_in_overridden(obj) and not has_base:
                    fallback_used = True

        # 2) 既存バックアップを一括クリア
        for obj in plan.keys():
            if getattr(obj, "backup_materials", None):
                obj.backup_materials.clear()
            key = _backup_key(obj)
            if key in obj:
                try:
                    del obj[key]
                except Exception:
                    pass

        # 3) 計画どおりに新しいバックアップを書き込み（Pointer方式）
        for obj, mats in plan.items():
            for m in mats:
                itm = obj.backup_materials.add()
                itm.material = m
            obj[_backup_key(obj)] = ""  # 旧方式互換フラグ

        msg = "ベース状態（オーバーライド無視）でマテリアルバックアップを更新しました"
        if fallback_used:
            msg += "（一部オブジェクトは既存ベースが無かったため“現在の状態”で保存）"
        self.report({'INFO'}, msg)
        return {'FINISHED'}


# material_override.py

class VLM_OT_force_backup_materials(bpy.types.Operator):
    bl_idname = "vlm.force_backup_materials"
    bl_label  = "マテリアルバックアップ"
    bl_description = (
        "今の見た目（現在のスロット割当）をそのまま保存します。\n"
        "ビューレイヤーは切り替えません。オーバーライドが見えている場合は、"
        "その状態を保存します。"
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        # どのビューレイヤーでも押せるように緩める
        try:
            return (context is not None and context.scene is not None)
        except Exception:
            return False

    def execute(self, context):
        # 1) 既存バックアップを全消去（PointerProperty も旧キーも）
        for obj in bpy.data.objects:
            if obj.type != 'MESH':
                continue
            # PointerProperty（新方式）
            if getattr(obj, "backup_materials", None):
                obj.backup_materials.clear()
            # 旧形式の文字列キー
            key = _backup_key(obj)
            if key in obj:
                try:
                    del obj[key]
                except Exception:
                    pass

        # 2) 今の見た目をそのまま保存（上書き）
        #    ※ backup_all_materials() は「未保存のものだけ」保存する仕様なので、
        #       先にクリアしてから呼び出すのがポイント
        backup_all_materials()  # ←「現在のスロット割当」を保存します

        self.report({'INFO'}, "現在の状態でマテリアルバックアップを更新しました")
        return {'FINISHED'}

class VLM_OT_restore_materials(bpy.types.Operator):
    bl_idname = "vlm.restore_materials"
    bl_label  = "マテリアルを復元"
    bl_description = "バックアップからマテリアルを復元"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        restore_all_materials()
        self.report({'INFO'}, "マテリアルを復元しました")
        return {'FINISHED'}

class VLM_OT_clear_backup_materials(bpy.types.Operator):
    bl_idname = "vlm.clear_backup_materials"
    bl_label  = "マテリアルバックアップをクリア"
    bl_description = "保存されたマテリアルバックアップデータをすべて削除します"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        try:
            sc = context.scene
            return context.window.view_layer == sc.view_layers[0]
        except Exception:
            return False

    def execute(self, context):
        from .material_override import _backup_key
        removed = 0
        for obj in bpy.data.objects:
            if _backup_key(obj) in obj:
                del obj[_backup_key(obj)]
                removed += 1
        self.report({'INFO'}, f"クリアされたオブジェクト数: {removed}")
        return {'FINISHED'}

# --------------------------------------------------
# レンダリング直前ハンドラ
# --------------------------------------------------
@persistent
def _render_pre(scene):
    """レンダリング開始直前に呼ばれるハンドラ
    
    他のアドオンの影響を受けないよう、最後に実行されることを前提とした処理
    1. 現在のマテリアル状態をバックアップ
    2. 選択的にマテリアルを復元・オーバーライド適用
    3. ライト／カメラ可視設定を適用
    """
    from . import light_camera
    
    # 初回バックアップ作成（既存があれば上書きしない）
    backup_all_materials()
    
    # オーバーライド指定があるオブジェクトのみ選択的に処理
    _apply_selective_material_overrides(bpy.context.view_layer)
    
    # ライト設定適用
    # ★ light_camera.py に apply_lights_visibility 関数が存在しないためコメントアウト
    # light_camera.apply_lights_visibility(bpy.context)
    # 代わりに apply_lights_for_viewlayer を使うのが適切かもしれません
    light_camera.apply_lights_for_viewlayer(bpy.context.view_layer)


# --------------------------------------------------
# register / unregister
# --------------------------------------------------
classes = (
    COLMATERIAL_OT_set_collection_override,
    COLMATERIAL_OT_clear_collection_override,
    VLM_OT_backup_materials_current,
    VLM_OT_backup_materials_base,
    VLM_OT_force_backup_materials,
    VLM_OT_restore_materials,
)

def register():
    def _safe_register_class(cls):
        try:
            bpy.utils.register_class(cls)
        except (ValueError, RuntimeError):
            pass

    # ① PropertyGroup 登録
    _safe_register_class(MaterialBackupItem)
    # ② Object にコレクションプロパティを追加
    if not hasattr(bpy.types.Object, "backup_materials"):
        bpy.types.Object.backup_materials = CollectionProperty(type=MaterialBackupItem)

    # 既存の operators 登録
    for c in classes:
        _safe_register_class(c)

    # ── render_pre ハンドラを最後に追加 ──
    #if _render_pre not in bpy.app.handlers.render_pre:
    #    bpy.app.handlers.render_pre.append(_render_pre)

    # ── depsgraph 更新ハンドラを追加 ──
    if _viewlayer_changed_handler not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_viewlayer_changed_handler)


def unregister():
    # render_pre ハンドラ解除
    if _render_pre in bpy.app.handlers.render_pre:
        bpy.app.handlers.render_pre.remove(_render_pre)

    # depsgraph 更新ハンドラ解除
    if _viewlayer_changed_handler in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_viewlayer_changed_handler)

    # operators のアンレジスターや CollectionProperty の削除など…
    def _safe_unregister_class(cls):
        try:
            bpy.utils.unregister_class(cls)
        except (ValueError, RuntimeError):
            pass

    for c in reversed(classes):
        _safe_unregister_class(c)
    if hasattr(bpy.types.Object, "backup_materials"):
        del bpy.types.Object.backup_materials
    _safe_unregister_class(MaterialBackupItem)
