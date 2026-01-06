import bpy

from . import (
    light_camera          as lc,
    collection_management as colm,
    render_override       as ro,
)


class VLM_PG_viewlayer_target(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty(name="ViewLayer Name")
    selected: bpy.props.BoolProperty(name="Selected", default=False)


STATE_ITEMS = [
    ('KEEP', "-", "現在の状態を維持"),
    ('ON',   "ON",     "ON にする"),
    ('OFF',  "OFF",    "OFF にする"),
]

ENGINE_LABELS = {key: label for key, label, _ in ro.ENGINES}


class VLM_PG_collection_multi_state(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty(name="Collection Name")
    level: bpy.props.IntProperty(name="Depth", default=0)

    content_state: bpy.props.EnumProperty(name="内容", items=STATE_ITEMS, default='KEEP')
    select_state: bpy.props.EnumProperty(name="選択", items=STATE_ITEMS, default='KEEP')
    render_state: bpy.props.EnumProperty(name="レンダー", items=STATE_ITEMS, default='KEEP')
    holdout_state: bpy.props.EnumProperty(name="ホールドアウト", items=STATE_ITEMS, default='KEEP')
    indirect_state: bpy.props.EnumProperty(name="間接的のみ", items=STATE_ITEMS, default='KEEP')


class VLM_PG_collection_toggle(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty(name="Collection Name")
    enabled: bpy.props.BoolProperty(name="Enabled", default=True)
    level: bpy.props.IntProperty(name="Depth", default=0)


class VLM_PG_render_layer_entry(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty(name="ViewLayer Name")

    engine_enable: bpy.props.BoolProperty(name="エンジンを上書き", default=False)
    engine: bpy.props.EnumProperty(name="エンジン", items=ro.ENGINES, default="BLENDER_EEVEE_NEXT")

    samples_enable: bpy.props.BoolProperty(name="サンプル数を上書き", default=False)
    samples: bpy.props.IntProperty(name="サンプル数", default=64, min=1, max=4096)

    camera_enable: bpy.props.BoolProperty(name="カメラを上書き", default=False)
    camera: bpy.props.PointerProperty(name="カメラ", type=bpy.types.Object, poll=lambda _self, obj: bool(obj) and obj.type == 'CAMERA')

    world_enable: bpy.props.BoolProperty(name="World を上書き", default=False)
    world: bpy.props.PointerProperty(name="World", type=bpy.types.World)

    format_enable: bpy.props.BoolProperty(name="フォーマットを上書き", default=False)
    resolution_x: bpy.props.IntProperty(name="解像度X", default=1920, min=1, max=16384)
    resolution_y: bpy.props.IntProperty(name="解像度Y", default=1080, min=1, max=16384)
    resolution_percentage: bpy.props.IntProperty(name="スケール(%)", default=100, min=1, max=1000)
    aspect_x: bpy.props.FloatProperty(name="アスペクトX", default=1.0, min=0.1)
    aspect_y: bpy.props.FloatProperty(name="アスペクトY", default=1.0, min=0.1)
    frame_rate: bpy.props.FloatProperty(name="FPS", default=24.0, min=0.01, max=60000.0, precision=3)

    frame_enable: bpy.props.BoolProperty(name="フレーム範囲を上書き", default=False)
    frame_start: bpy.props.IntProperty(name="開始フレーム", default=1, min=0)
    frame_end: bpy.props.IntProperty(name="終了フレーム", default=250, min=0)
    frame_step: bpy.props.IntProperty(name="フレームステップ", default=1, min=1)


def _gather_shader_aovs_from_tree(nt, visited):
    """ノードツリー内の AOV 出力名とタイプを収集（ノードグループも再帰）"""
    if nt is None or nt in visited:
        return {}
    visited.add(nt)

    found = {}
    for node in getattr(nt, "nodes", []):
        # シェーダー AOV 出力
        if getattr(node, "bl_idname", "") == "ShaderNodeOutputAOV":
            raw = (getattr(node, "name", "") or "").strip()
            if not raw:
                raw = (getattr(node, "label", "") or "").strip()
            if raw:
                aov_type = getattr(node, "type", "COLOR") or "COLOR"
                # 既に同名があれば最初のタイプを優先
                found.setdefault(raw, aov_type)
        # ノードグループを再帰的に探索
        elif getattr(node, "type", "") == 'GROUP':
            sub_tree = getattr(node, "node_tree", None)
            if sub_tree:
                sub = _gather_shader_aovs_from_tree(sub_tree, visited)
                for k, v in sub.items():
                    found.setdefault(k, v)
    return found


def _gather_shader_aovs_from_material(mat):
    """マテリアルに含まれる AOV 出力名を (名前→タイプ) で返す"""
    if mat is None or not getattr(mat, "use_nodes", False):
        return {}
    nt = getattr(mat, "node_tree", None)
    if nt is None:
        return {}
    return _gather_shader_aovs_from_tree(nt, set())


def _collect_aov_names_for_view_layer(vl):
    """ビューレイヤーに表示されているオブジェクトの AOV 名を集計"""
    if vl is None:
        return {}

    names = {}
    objs = getattr(vl, "objects", [])
    for obj in objs:
        # ビューレイヤーで可視なオブジェクトのみ対象
        try:
            if not obj.visible_get(view_layer=vl):
                continue
        except TypeError:
            if not obj.visible_get():
                continue
        for slot in getattr(obj, "material_slots", []):
            mat = getattr(slot, "material", None)
            if mat is None:
                continue
            for nm, tp in _gather_shader_aovs_from_material(mat).items():
                names.setdefault(nm, tp)
    return names


def _ensure_view_layer_aovs(vl, aov_map):
    """ビューレイヤーに指定された AOV 名を追加（既存はスキップ）"""
    if vl is None or not hasattr(vl, "aovs"):
        return 0
    existing = {aov.name for aov in getattr(vl, "aovs", [])}
    added = 0
    for nm, tp in aov_map.items():
        if not nm or nm in existing:
            continue
        try:
            aov = vl.aovs.new()
        except Exception:
            aov = vl.aovs.add()
        aov.name = nm
        if hasattr(aov, "type"):
            try:
                aov.type = tp
            except Exception:
                pass
        existing.add(nm)
        added += 1
    return added


def _fold(layout, owner, prop_name, title):
    """折りたたみ安全版：プロパティが無い場合でもUIが落ちない"""
    is_open = bool(getattr(owner, prop_name, False))
    row = layout.row(align=True)
    icon = 'TRIA_DOWN' if is_open else 'TRIA_RIGHT'
    if hasattr(owner, prop_name):
        row.prop(owner, prop_name, text=title, icon=icon, emboss=False)
    else:
        row.label(text=title, icon=icon)
    return is_open

# ──────────────────────────────────────────────
# 追加オペ：出力ノード作成＋AO乗算（任意）
# ──────────────────────────────────────────────
class VLM_OT_prepare_output_nodes_plus(bpy.types.Operator):
    """既存の 'vlm.prepare_output_nodes' を実行した後、
    Scene.vlm_enable_ao_multiply がONなら AO×Image の乗算チェーンを追加する。
    """
    bl_idname = "vlm.prepare_output_nodes_plus"
    bl_label  = "ノードを作成・接続"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        sc = context.scene
        # まず既存の接続ルーチンを実行
        try:
            bpy.ops.vlm.prepare_output_nodes()
        except Exception as e:
            self.report({'ERROR'}, f"既存のノード作成に失敗: {e}")
            return {'CANCELLED'}

        # チェックOFFならここで終わり
        if not bool(getattr(sc, "vlm_enable_ao_multiply", False)):
            self.report({'INFO'}, "出力ノードを作成・接続しました")
            return {'FINISHED'}

        try:
            self._apply_ao_multiply_chain(context)
        except Exception as e:
            self.report({'WARNING'}, f"AO乗算の適用で一部失敗: {e}")
        else:
            self.report({'INFO'}, "出力ノード＋AO乗算を適用しました")

        return {'FINISHED'}

    # === 実装本体 ===
    def _apply_ao_multiply_chain(self, context):
        sc = context.scene
        # コンポジットON
        sc.use_nodes = True
        nt = sc.node_tree
        if nt is None:
            raise RuntimeError("Scene.node_tree が見つかりません")

        # 既存の Render Layers ノード群を対象
        rlayers = [n for n in nt.nodes if n.type == 'R_LAYERS']
        if not rlayers:
            raise RuntimeError("Render Layers ノードが見つかりません")

        for rl in rlayers:
            # 1) 対応する ViewLayer の AO パスを有効化
            vl_name = getattr(rl, "layer", None)
            vl = sc.view_layers.get(vl_name) if vl_name else None
            if vl and hasattr(vl, "use_pass_ambient_occlusion"):
                if not vl.use_pass_ambient_occlusion:
                    vl.use_pass_ambient_occlusion = True

            # AO ソケット確認（パスON後に追加される）
            sock_img = rl.outputs.get("Image")
            sock_ao  = rl.outputs.get("AO")
            if sock_img is None:
                # “画像”が無いレイヤーは対象外
                continue
            if sock_ao is None:
                # AO パスが無いレンダーエンジン等ではスキップ
                continue

            # すでに当該レイヤーに VLM 管理のMixがあるなら再配線は避ける
            mix_tag = f"VLM_AO_Mix__{vl_name or rl.name}"
            curves_tag = f"VLM_AO_Curves__{vl_name or rl.name}"
            existing_mix = next((n for n in nt.nodes
                                 if n.type == 'MIX_RGB' and getattr(n, "label", "") == mix_tag), None)
            if existing_mix:
                # 既に適用済みとみなす
                continue

            # 2) MixRGB（乗算）と RGB Curves を追加
            mix = nt.nodes.new("CompositorNodeMixRGB")
            mix.blend_type = 'MULTIPLY'
            mix.inputs[0].default_value = 1.0  # Fac=1
            mix.label = mix_tag
            mix["vlm_ao_multiply"] = True

            curves = nt.nodes.new("CompositorNodeCurveRGB")
            curves.label = curves_tag
            curves["vlm_ao_multiply"] = True

            # 位置：元の RenderLayers の右側に整列
            mix.location    = (rl.location.x + 200, rl.location.y - 40)
            curves.location = (rl.location.x + 200, rl.location.y - 180)

            # 3) 画像 → MixのA、 AO → Curves → MixのB
            # 既存の Image 出力リンクを退避
            old_links = list(sock_img.links)
            for link in old_links:
                nt.links.remove(link)
            # AO -> Curves
            nt.links.new(sock_ao, curves.inputs.get("Image", curves.inputs[0]))
            # RLayer Image -> Mix A
            nt.links.new(sock_img, mix.inputs.get("Color1", mix.inputs[1]))
            # Curves -> Mix B
            nt.links.new(curves.outputs.get("Image", curves.outputs[0]), mix.inputs.get("Color2", mix.inputs[2]))

            # 4) Curves（合成チャンネル）にポイントを追加
            mp = curves.mapping
            mp.use_clip = False
            # Combined（4番目が合成チャンネルのはず）
            try:
                ccurve = mp.curves[3]
            except Exception:
                ccurve = mp.curves[-1]
            # 既存のデフォルト(0,0)(1,1)は残したまま2点を追加
            # 同一点が存在する場合は追加をスキップ
            def has_point(x, y, eps=1e-4):
                return any(abs(p.location[0]-x) < eps and abs(p.location[1]-y) < eps for p in ccurve.points)
            if not has_point(0.2, 0.2):
                ccurve.points.new(0.2, 0.2)
            if not has_point(0.64, 0.8):
                ccurve.points.new(0.64, 0.8)
            mp.update()

            # 5) Mix出力を旧リンク先へ一括再配線
            out_sock = mix.outputs.get("Image", mix.outputs[0])
            for link in old_links:
                try:
                    nt.links.new(out_sock, link.to_socket)
                except Exception:
                    pass


class VLM_OT_duplicate_viewlayers_popup(bpy.types.Operator):
    bl_idname = "vlm.duplicate_viewlayers_popup"
    bl_label  = "ビューレイヤーを複製"
    bl_options = {'REGISTER', 'UNDO'}

    viewlayers: bpy.props.CollectionProperty(type=VLM_PG_viewlayer_target)
    collections: bpy.props.CollectionProperty(type=VLM_PG_collection_toggle)
    rename_from: bpy.props.StringProperty(name="置換元", description="名前の一部を置換する場合に指定")
    rename_to: bpy.props.StringProperty(name="置換先", description="置換後の文字列")
    name_prefix: bpy.props.StringProperty(name="プレフィックス", description="名前の頭に付ける文字列")
    name_suffix: bpy.props.StringProperty(name="サフィックス", description="名前の末尾に付ける文字列")
    custom_name: bpy.props.StringProperty(name="指定名", description="ここに入力するとこの名前を元に複製します")

    def _collect_layer_collections(self, root, level=0):
        entry = self.collections.add()
        entry.name = root.collection.name
        entry.enabled = not bool(root.exclude)
        entry.level = level
        for child in root.children:
            self._collect_layer_collections(child, level + 1)

    def invoke(self, context, event):
        self.viewlayers.clear(); self.collections.clear()
        sc = context.scene
        active_name = context.view_layer.name if context.view_layer else ""

        for vl in sc.view_layers:
            item = self.viewlayers.add()
            item.name = vl.name
            item.selected = (vl.name == active_name)

        if context.view_layer:
            self._collect_layer_collections(context.view_layer.layer_collection, 0)

        return context.window_manager.invoke_props_dialog(self, width=420)

    def draw(self, context):
        layout = self.layout
        layout.label(text="複製するビューレイヤー", icon='RENDERLAYERS')
        box = layout.box()
        for item in self.viewlayers:
            row = box.row(align=True)
            row.prop(item, "selected", text="")
            row.label(text=item.name, icon='RENDERLAYERS')

        layout.separator()
        layout.label(text="名前置換 (任意)", icon='SORTALPHA')
        name_box = layout.box()
        row = name_box.row(align=True)
        row.prop(self, "rename_from", text="置換元")
        row.prop(self, "rename_to", text="置換先")
        row = name_box.row(align=True)
        row.prop(self, "name_prefix", text="プレフィックス")
        row.prop(self, "name_suffix", text="サフィックス")
        name_box.prop(self, "custom_name", text="指定名")
        hint = name_box.box()
        hint.label(text="例: glay→bl として複製すると alp_glay_C1 → alp_bl_C1", icon='INFO')

        layout.separator()
        layout.label(text="複製後にONにするコレクション", icon='OUTLINER_COLLECTION')
        cbox = layout.box()
        for coll in self.collections:
            row = cbox.row(align=True)
            row.separator(factor=0.4 + 0.2 * coll.level)
            row.separator(factor=0.8 + 0.4 * coll.level)
            row.prop(coll, "enabled", text="", toggle=True)
            row.label(text=coll.name, icon='OUTLINER_COLLECTION')

        info = layout.box()
        info.label(text="チェック=含める / OFF=除外として複製", icon='INFO')

    def execute(self, context):
        sc = context.scene
        targets = [i.name for i in self.viewlayers if i.selected]
        if not targets:
            targets = [context.view_layer.name] if context.view_layer else []
        if not targets:
            self.report({'WARNING'}, "複製するビューレイヤーを選択してください")
            return {'CANCELLED'}

        states = {c.name: c.enabled for c in self.collections}
        rename_from = (self.rename_from or "").strip()
        rename_to = self.rename_to or ""
        name_prefix = self.name_prefix or ""
        name_suffix = self.name_suffix or ""
        custom_name = (self.custom_name or "").strip()
        created = []
        for name in targets:
            src = sc.view_layers.get(name)
            if not src:
                continue
            base = custom_name if custom_name else name
            if rename_from and not custom_name:
                replaced = base.replace(rename_from, rename_to)
                base = replaced if replaced else base
            base = f"{name_prefix}{base}{name_suffix}"

            new_vl = colm.duplicate_view_layer_with_collections(
                sc,
                src,
                collection_states=states,
                desired_name=base,
            )
            if new_vl:
                created.append((name, new_vl.name))

        if not created:
            self.report({'WARNING'}, "複製に失敗しました")
            return {'CANCELLED'}

        state_txt = ", ".join([f"{k}:{'ON' if v else 'OFF'}" for k, v in states.items()]) or "コレクション設定なし"
        layer_txt = ", ".join([f"{src}→{dst}" for src, dst in created])
        rename_txt = "" if not rename_from else f" / 名前置換: '{rename_from}'→'{rename_to}'"
        self.report({'INFO'}, f"複製完了: {layer_txt} / {state_txt}{rename_txt}")
        return {'FINISHED'}


class VLM_OT_apply_collection_settings_popup(bpy.types.Operator):
    bl_idname = "vlm.apply_collection_settings_popup"
    bl_label  = "コレクション設定を一括適用"
    bl_options = {'REGISTER', 'UNDO'}

    viewlayers: bpy.props.CollectionProperty(type=VLM_PG_viewlayer_target)
    collection_rules: bpy.props.CollectionProperty(type=VLM_PG_collection_multi_state)

    def _collect_layer_collections(self, lc, level=0):
        if lc.collection.name in {"Scene Collection", "シーンコレクション"}:
            for child in lc.children:
                self._collect_layer_collections(child, level)
            return

        item = self.collection_rules.add()
        item.name = lc.collection.name
        item.level = level

        for child in lc.children:
            self._collect_layer_collections(child, level + 1)

    def invoke(self, context, event):
        self.viewlayers.clear()
        self.collection_rules.clear()

        sc = context.scene
        active_name = context.view_layer.name if context.view_layer else ""
        for vl in sc.view_layers:
            entry = self.viewlayers.add()
            entry.name = vl.name
            entry.selected = (vl.name == active_name)

        if context.view_layer:
            self._collect_layer_collections(context.view_layer.layer_collection, 0)

        # ここを変更（元は width=560 など）
        return context.window_manager.invoke_props_dialog(self, width=500)


    def draw(self, context):
        layout = self.layout

        # --- ビューレイヤー選択部 ---
        layout.label(text="適用対象のビューレイヤー", icon='RENDERLAYERS')
        lbox = layout.box()
        for item in self.viewlayers:
            row = lbox.row(align=True)
            row.prop(item, "selected", text="")
            row.label(text=item.name, icon='RENDERLAYERS')

        layout.separator()
        layout.label(text="コレクション設定", icon='OUTLINER_COLLECTION')
        cbox = layout.box()

        titles = ("内容", "選択", "レンダー", "ホールドアウト", "間接的のみ")
        prop_names = (
            "content_state",
            "select_state",
            "render_state",
            "holdout_state",
            "indirect_state",
        )

        # 左側「コレクション名」列の幅（ここを固定することで、ヘッダーと本体の開始位置を揃える）
        NAME_WIDTH  = 12.0

        # 1グループ（3ボタン）の横幅
        # WS000572.PNG でちょうど良かったサイズに合わせて 1.0 にしています
        GROUP_WIDTH = 3.0

        # グループ間の隙間
        GROUP_GAP   = 0.4

        # =========================
        # ヘッダー行
        # =========================
        head = cbox.row(align=True)

        # 左：コレクション名ヘッダー（固定幅）
        name_head = head.row(align=True)
        name_head.ui_units_x = NAME_WIDTH
        name_head.label(text="コレクション")

        # 右：状態ヘッダー（下のボタン列と同じ構造）
        states_head = head.row(align=True)

        for i, title in enumerate(titles):
            g = states_head.row(align=True)
            g.ui_units_x = GROUP_WIDTH
            g.alignment = 'CENTER'
            g.label(text=title)

            if i != len(titles) - 1:
                states_head.separator(factor=GROUP_GAP)

        # =========================
        # 各コレクション行
        # =========================
        for rule in self.collection_rules:
            row = cbox.row(align=True)

            # 左：コレクション名（ヘッダーと同じ NAME_WIDTH を使う）
            name_row = row.row(align=True)
            name_row.ui_units_x = NAME_WIDTH
            name_row.separator(factor=0.4 + 0.2 * rule.level)
            name_row.label(text=rule.name, icon='OUTLINER_COLLECTION')

            # 右：状態ボタン列（ヘッダーと同じ並び）
            states_row = row.row(align=True)

            for i, pname in enumerate(prop_names):
                g = states_row.row(align=True)
                g.ui_units_x = GROUP_WIDTH

                # 3ボタン（Enum + expand） → ドラッグ操作は今までどおり
                g.prop(rule, pname, expand=True, text="")

                if i != len(prop_names) - 1:
                    states_row.separator(factor=GROUP_GAP)

        hint = layout.box()
        hint.label(
            text="ON/OFF を選んだ項目のみ変更します。変更なしは現状維持。",
            icon='INFO'
        )


    def execute(self, context):
        sc = context.scene
        targets = [i.name for i in self.viewlayers if i.selected]
        if not targets and context.view_layer:
            targets = [context.view_layer.name]
        if not targets:
            self.report({'WARNING'}, "ビューレイヤーを選択してください")
            return {'CANCELLED'}

        def _state(val, on_val=True, off_val=False):
            if val == 'KEEP':
                return None
            return on_val if val == 'ON' else off_val

        settings = {}
        for rule in self.collection_rules:
            conf = {
                "include": _state(rule.content_state, True, False),
                "select": _state(rule.select_state, True, False),
                "render": _state(rule.render_state, True, False),
                "holdout": _state(rule.holdout_state, True, False),
                "indirect_only": _state(rule.indirect_state, True, False),
            }
            if any(v is not None for v in conf.values()):
                settings[rule.name] = conf

        if not settings:
            self.report({'WARNING'}, "変更内容が選択されていません")
            return {'CANCELLED'}

        applied_layers, touched_cols = colm.apply_collection_settings(sc, targets, settings)

        try:
            context.view_layer.update()
        except Exception:
            pass

        if not applied_layers:
            self.report({'WARNING'}, "適用できるコレクションが見つかりませんでした")
            return {'CANCELLED'}

        layer_txt = ", ".join([f"{nm}({len(cols)}件)" for nm, cols in applied_layers])
        cols_txt = ", ".join(touched_cols) if touched_cols else "なし"
        self.report({'INFO'}, f"設定を適用: {layer_txt} / 変更コレクション: {cols_txt}")
        return {'FINISHED'}


class VLM_OT_apply_render_settings_popup(bpy.types.Operator):
    bl_idname = "vlm.apply_render_settings_popup"
    bl_label  = "レンダー設定を一括適用"
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        layers = getattr(context.window_manager, "vlm_render_layers", None)
        if layers is None:
            self.report({'ERROR'}, "レンダー設定用の一時コレクションが初期化されていません")
            return {'CANCELLED'}

        layers.clear()
        sc = context.scene

        for vl in sc.view_layers:
            rs = getattr(vl, "vlm_render", None)
            if rs is None:
                continue

            entry = layers.add()
            entry.name = vl.name

            entry.engine_enable = bool(getattr(rs, "engine_enable", False))
            entry.engine = getattr(rs, "engine", entry.engine)

            entry.samples_enable = bool(getattr(rs, "samples_enable", False))
            entry.samples = getattr(rs, "samples", entry.samples)

            entry.camera_enable = bool(getattr(rs, "camera_enable", False))
            entry.camera = getattr(rs, "camera", None)

            entry.world_enable = bool(getattr(rs, "world_enable", False))
            entry.world = getattr(vl, "vlm_world", None) or sc.world

            entry.format_enable = bool(getattr(rs, "format_enable", False))
            entry.resolution_x = getattr(rs, "resolution_x", sc.render.resolution_x)
            entry.resolution_y = getattr(rs, "resolution_y", sc.render.resolution_y)
            entry.resolution_percentage = getattr(rs, "resolution_percentage", sc.render.resolution_percentage)
            entry.aspect_x = getattr(rs, "aspect_x", sc.render.pixel_aspect_x)
            entry.aspect_y = getattr(rs, "aspect_y", sc.render.pixel_aspect_y)
            entry.frame_rate = getattr(rs, "frame_rate", sc.render.fps / sc.render.fps_base)

            entry.frame_enable = bool(getattr(rs, "frame_enable", False))
            entry.frame_start = getattr(rs, "frame_start", sc.frame_start)
            entry.frame_end = getattr(rs, "frame_end", sc.frame_end)
            entry.frame_step = getattr(rs, "frame_step", sc.frame_step)

        return context.window_manager.invoke_props_dialog(self, width=980)

    def draw(self, context):
        layout = self.layout
        layout.label(text="レンダー設定一覧", icon='RENDER_STILL')
        box = layout.box()

        layers = getattr(context.window_manager, "vlm_render_layers", None)
        if layers is None:
            layout.label(text="(データを初期化できませんでした)", icon='ERROR')
            return

        for entry in layers:
            row = box.row(align=True)
            name_cell = row.row(align=True)
            name_cell.ui_units_x = 8.0
            name_cell.label(text=entry.name, icon='RENDERLAYERS')

            erow = row.row(align=True)
            erow.prop(entry, "engine_enable", text="")
            evals = erow.row(align=True)
            evals.ui_units_x = 10.0
            evals.prop(entry, "engine", text="")

            srow = row.row(align=True)
            srow.prop(entry, "samples_enable", text="")
            sval = srow.row(align=True)
            sval.enabled = bool(entry.samples_enable)
            sval.ui_units_x = 6.0
            sval.prop(entry, "samples", text="サンプル数")

            crow = row.row(align=True)
            crow.prop(entry, "camera_enable", text="")
            cam_val = crow.row(align=True)
            cam_val.enabled = bool(entry.camera_enable)
            cam_val.ui_units_x = 18.0
            cam_val.prop(entry, "camera", text="")

            wrow = row.row(align=True)
            wrow.prop(entry, "world_enable", text="")
            wval = wrow.row(align=True)
            wval.enabled = bool(entry.world_enable)
            wval.ui_units_x = 18.0
            wval.prop(entry, "world", text="")

            frow = row.row(align=True)
            frow.prop(entry, "format_enable", text="")
            fvals = frow.row(align=True)
            fvals.enabled = bool(entry.format_enable)
            fvals.ui_units_x = 24.0
            fvals.prop(entry, "resolution_x", text="X")
            fvals.prop(entry, "resolution_y", text="Y")
            fvals.prop(entry, "resolution_percentage", text="スケール")
            fvals.prop(entry, "aspect_x", text="アスペクトX")
            fvals.prop(entry, "aspect_y", text="アスペクトY")
            fvals.prop(entry, "frame_rate", text="FPS")

            frrow = row.row(align=True)
            frrow.prop(entry, "frame_enable", text="")
            frvals = frrow.row(align=True)
            frvals.enabled = bool(entry.frame_enable)
            frvals.ui_units_x = 14.0
            frvals.prop(entry, "frame_start", text="開始")
            frvals.prop(entry, "frame_end", text="終了")
            frvals.prop(entry, "frame_step", text="ステップ")

    def execute(self, context):
        sc = context.scene
        applied = []
        layers = getattr(context.window_manager, "vlm_render_layers", None)
        if layers is None:
            self.report({'ERROR'}, "レンダー設定を取得できませんでした")
            return {'CANCELLED'}

        for entry in layers:
            vl = sc.view_layers.get(entry.name)
            if vl is None:
                continue
            rs = getattr(vl, "vlm_render", None)
            if rs is None:
                continue

            rs.engine_enable = bool(entry.engine_enable)
            rs.engine = entry.engine

            rs.samples_enable = bool(entry.samples_enable)
            rs.samples = entry.samples

            rs.camera_enable = bool(entry.camera_enable)
            rs.camera = entry.camera if entry.camera_enable else rs.camera

            rs.world_enable = bool(entry.world_enable)
            if entry.world_enable:
                vl.vlm_world = entry.world

            rs.format_enable = bool(entry.format_enable)
            rs.resolution_x = entry.resolution_x
            rs.resolution_y = entry.resolution_y
            rs.resolution_percentage = entry.resolution_percentage
            rs.aspect_x = entry.aspect_x
            rs.aspect_y = entry.aspect_y
            rs.frame_rate = entry.frame_rate

            rs.frame_enable = bool(entry.frame_enable)
            rs.frame_start = entry.frame_start
            rs.frame_end = entry.frame_end
            rs.frame_step = entry.frame_step

            applied.append(entry.name)

        if not applied:
            self.report({'WARNING'}, "適用できるビューレイヤーがありません")
            return {'CANCELLED'}

        try:
            ro.apply_render_override(sc, context.view_layer)
        except Exception:
            pass

        self.report({'INFO'}, f"レンダー設定を適用: {', '.join(applied)}")
        return {'FINISHED'}


class VLM_OT_add_shader_aovs(bpy.types.Operator):
    """表示中オブジェクトのマテリアルにある AOV 出力をビューレイヤーに追加する"""
    bl_idname = "vlm.add_shader_aovs"
    bl_label  = "シェーダーAOVを追加"
    bl_options = {'REGISTER', 'UNDO'}

    apply_all: bpy.props.BoolProperty(
        name="全てのビューレイヤーに追加",
        description="全ビューレイヤーの表示オブジェクトを走査して AOV を追加する",
        default=False,
    )

    def execute(self, context):
        sc = context.scene
        targets = list(sc.view_layers) if self.apply_all else [context.view_layer] if context.view_layer else []
        if not targets:
            self.report({'WARNING'}, "ビューレイヤーが見つかりません")
            return {'CANCELLED'}

        total_added = 0
        per_layer = []
        for vl in targets:
            aov_map = _collect_aov_names_for_view_layer(vl)
            if not aov_map:
                continue
            added = _ensure_view_layer_aovs(vl, aov_map)
            if added:
                total_added += added
                per_layer.append(f"{vl.name}:{added}")

        if not total_added:
            self.report({'INFO'}, "追加できる AOV はありませんでした")
            return {'CANCELLED'}

        detail = ", ".join(per_layer) if per_layer else "なし"
        self.report({'INFO'}, f"シェーダーAOVを追加しました ({detail})")
        return {'FINISHED'}


class VLM_PT_panel(bpy.types.Panel):
    bl_label       = "View Layer Manager (Light & Collection)"
    bl_idname      = "VLM_PT_panel"
    bl_space_type  = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category    = "RenderLayers"

    def draw(self, context):
        layout = self.layout
        sc = context.scene
        rs = context.scene.render
        vl = context.view_layer

        # ビューレイヤー
        vlayers = context.scene.view_layers
        curr = vl.name if vlayers else ""
        is_top_layer = (vlayers and vl.name == vlayers[0].name)

        # 1) アクティブ ViewLayer 情報
        row = layout.row(align=True)
        row.label(text=f"Active ViewLayer: {curr}", icon='RENDERLAYERS')
        layout.separator()

        # 1.1) ViewLayer 一覧
        for v in vlayers:
            row = layout.row(align=True)
            if hasattr(v, "vlm_render_this_layer"):
                row.prop(v, "vlm_render_this_layer", text="")
            else:
                row.label(text="", icon='BLANK1')
            row.prop(v, "name", text="", emboss=False, icon='RENDERLAYERS')
            if v.name == curr:
                row.label(text="", icon='RADIOBUT_ON')
            else:
                op = row.operator("vlm.set_active_viewlayer", text="", icon='RADIOBUT_OFF', emboss=False)
                op.layer_name = v.name

        dup_row = layout.row(align=True)
        dup_row.operator("vlm.duplicate_viewlayers_popup", icon='DUPLICATE')
        dup_row.operator("vlm.apply_collection_settings_popup", icon='MODIFIER_ON')
        dup_row.operator("vlm.apply_render_settings_popup", icon='RENDER_STILL')

        layout.separator()

        # 2) コレクション（既存）
        if _fold(layout, sc, "vlm_ui_show_collections", "コレクション"):
            if vlayers:
                root = vl.layer_collection
                self._draw_collections(layout, root, curr, is_top_layer, depth=0)
            layout.separator()

        # 3) マテリアルバックアップ（既存）
        if _fold(layout, sc, "vlm_ui_show_mat_backup", "マテリアルバックアップ"):
            row = layout.row(align=True)
            row.operator("vlm.backup_materials_current", icon='FILE_TICK', text="今の見た目を保存")
            row.operator("vlm.backup_materials_base",    icon='FILE_TICK', text="ベース状態を保存")
            row.operator("vlm.clear_backup_materials",   icon='TRASH',     text="クリア")
            layout.separator()

        # ─────────────────────────────────────────
        # ① サンプルレンダリング（全VLを強制上書き）
        # ─────────────────────────────────────────
        if _fold(layout, sc, "vlm_ui_show_sample_override", "サンプルレンダリング（全ビューレイヤーのサンプル数を強制上書き）"):
            row = layout.row(align=True)
            if hasattr(sc, "vlm_force_samples_enable"):
                row.prop(sc, "vlm_force_samples_enable", text="サンプルレンダリングを有効にする")

                col = layout.column(align=True)
                col.enabled = bool(getattr(sc, "vlm_force_samples_enable", False))

                info = col.box()
                info.label(text="※ 有効時は全ビューレイヤーのサンプル数をこの値で上書きします。", icon='INFO')

                if hasattr(sc, "vlm_force_samples_cycles"):
                    col.prop(sc, "vlm_force_samples_cycles", text="Cycles Samples（強制）")
                if hasattr(sc, "vlm_force_samples_eevee"):
                    col.prop(sc, "vlm_force_samples_eevee",  text="Eevee Samples（強制）")

            layout.separator()

        # ─────────────────────────────────────────
        # ② レンダーエンジン（エンジンのみを選択：Samples/Denoiseは削除）
        # ─────────────────────────────────────────
        if _fold(layout, sc, "vlm_ui_show_render_engine", "レンダーエンジン"):
            try:
                vrs = getattr(context.view_layer, "vlm_render", None)
                vlayers = context.scene.view_layers
                is_top_layer = (vlayers and context.view_layer.name == vlayers[0].name)

                if vrs is None:
                    layout.label(text="(vlm_render が未登録です)", icon='ERROR')
                else:
                    # トップ以外は enable トグル、トップは常時基準
                    row = layout.row(align=True)
                    row.enabled = not is_top_layer
                    row.prop(vrs, "engine_enable", text="このレイヤーの値を使用")

                    col = layout.column(align=True)
                    col.enabled = (is_top_layer or vrs.engine_enable)
                    col.prop(vrs, "engine", text="Engine")
                    # ※ ここから Samples / Denoise は削除しました（エンジンのみ）
            except Exception as e:
                layout.label(text=f"Engine UI error: {e}", icon='ERROR')

            layout.separator()

        # ─────────────────────────────────────────
        # ③ 各ビューレイヤーのサンプル数（優先順位②）
        #    ※ 強制サンプルONのときはグレーアウト
        # ─────────────────────────────────────────
        vrs = getattr(context.view_layer, "vlm_render", None)
        if vrs is not None:
            box = layout.box()
            head = box.row(align=True)
            head.label(text="各ビューレイヤーのサンプル数（優先順位②）", icon='RENDERLAYERS')

            # 強制ONのときはグレーアウト
            box.enabled = not bool(getattr(sc, "vlm_force_samples_enable", False))

            has_enable = ("samples_enable" in getattr(vrs.__class__, "__annotations__", {})) or hasattr(vrs, "samples_enable")

            row = box.row(align=True)
            if has_enable:
                row.prop(vrs, "samples_enable", text="このレイヤーのサンプル数を使用")
            else:
                row.label(text="(samples_enable が未登録です。アドオンを再読み込みしてください)", icon='ERROR')

            s = row.row(align=True)
            s.enabled = has_enable and bool(getattr(vrs, "samples_enable", False))
            s.prop(vrs, "samples", text="サンプル数")

            # 先頭VL向けヒント
            vlayers = context.scene.view_layers
            if vlayers and context.view_layer.name == vlayers[0].name:
                hint = box.box()
                hint.label(text="先頭レイヤーのサンプル数は、他レイヤーのフォールバック値として使われます。", icon='INFO')

            layout.separator()

        # ─────────────────────────────────────────
        # ④ Cycles ライトパス
        # ─────────────────────────────────────────
        if _fold(layout, sc, "vlm_ui_show_cycles_light_paths", "Cycles ライトパス"):
            vrs = getattr(context.view_layer, "vlm_render", None)
            if vrs is None:
                layout.label(text="(vlm_render が未登録です)", icon='ERROR')
            else:
                row = layout.row(align=True)
                row.enabled = not is_top_layer
                row.prop(vrs, "light_paths_enable", text="このレイヤーの設定を使用")

                col = layout.column(align=True)
                col.enabled = (is_top_layer or bool(getattr(vrs, "light_paths_enable", False)))

                if getattr(vrs, "engine", "") != "CYCLES":
                    warn = col.box()
                    warn.label(text="Cycles 選択時のみ有効です", icon='INFO')

                max_box = col.box()
                max_box.label(text="最大バウンス数")
                max_box.prop(vrs, "light_path_max_bounces", text="合計")
                max_box.prop(vrs, "light_path_diffuse_bounces", text="ディフューズ")
                max_box.prop(vrs, "light_path_glossy_bounces", text="光沢")
                max_box.prop(vrs, "light_path_transmission_bounces", text="伝播")
                max_box.prop(vrs, "light_path_volume_bounces", text="ボリューム")
                max_box.prop(vrs, "light_path_transparent_bounces", text="透過")

                clamp_box = col.box()
                clamp_box.label(text="制限")
                clamp_box.prop(vrs, "light_path_clamp_direct", text="直接照明")
                clamp_box.prop(vrs, "light_path_clamp_indirect", text="間接照明")

                caustics_box = col.box()
                caustics_box.label(text="コースティクス")
                caustics_box.prop(vrs, "light_path_filter_glossy", text="光沢フィルター")
                caustics_row = caustics_box.row(align=True)
                caustics_row.prop(vrs, "light_path_caustics_reflective", text="反射")
                caustics_row.prop(vrs, "light_path_caustics_refractive", text="屈折")

            layout.separator()

        # 以降は既存のまま（カメラ／ライト／World／フォーマット／フレーム範囲／出力ノード／レンダー出力）
        # 5) カメラ
        if _fold(layout, sc, "vlm_ui_show_camera", "カメラ"):
            vrs = getattr(context.view_layer, "vlm_render", None)
            row = layout.row(align=True)
            if vrs is not None:
                if not is_top_layer:
                    row.prop(vrs, "camera_enable", text="")
                    cam_row = row.row()
                    cam_row.enabled = bool(getattr(vrs, "camera_enable", False))
                    cam_row.prop(vrs, "camera", text="")
                else:
                    row.prop(vrs, "camera", text="")
            else:
                row.label(text="(vlm_render が未登録です)", icon='ERROR')
            row = layout.row(align=True)
            row.operator("vlm.memorize_camera",        icon='FILE_TICK', text="このカメラを記憶")
            row.operator("vlm.apply_memorized_camera", icon='IMPORT',    text="記憶カメラを反映")
            layout.separator()

        # 6) ライト状態
        if _fold(layout, sc, "vlm_ui_show_lights", "ライト状態"):
            row = layout.row(align=True)
            row.operator("vlm.memorize_lights",        icon='FILE_TICK', text="この状態を記憶")
            row.operator("vlm.apply_memorized_lights", icon='IMPORT',    text="記憶状態を反映")
            layout.separator()

        # 6.5) World 環境
        if _fold(layout, sc, "vlm_ui_show_world", "World 環境"):
            vrs = getattr(context.view_layer, "vlm_render", None)
            vlayers = context.scene.view_layers
            is_top = (vlayers and context.view_layer.name == vlayers[0].name)
            if vrs is None:
                layout.label(text="(vlm_render が未登録です)", icon='ERROR')
            else:
                row = layout.row(align=True)
                row.enabled = not is_top
                row.prop(vrs, "world_enable", text="このレイヤーの World を使用")
                col = layout.column(align=True)
                col.enabled = (is_top or vrs.world_enable)
                col.prop(context.view_layer, "vlm_world", text="World")
            layout.separator()

        # 7) フォーマット
        if _fold(layout, sc, "vlm_ui_show_format", "フォーマット"):
            vrs = getattr(context.view_layer, "vlm_render", None)
            vlayers = context.scene.view_layers
            is_top_layer = (vlayers and context.view_layer.name == vlayers[0].name)

            if vrs is None:
                layout.label(text="(vlm_render が未登録です)", icon='ERROR')
            else:
                row = layout.row(align=True)
                row.enabled = not is_top_layer
                row.prop(vrs, "format_enable", text="このレイヤーの値を使用")

                col = layout.column(align=True)
                col.enabled = (is_top_layer or vrs.format_enable)
                col.prop(vrs, "resolution_x",          text="解像度 X")
                col.prop(vrs, "resolution_y",          text="解像度 Y")
                col.prop(vrs, "resolution_percentage", text="Scale (%)")
                col.prop(vrs, "aspect_x",              text="アスペクト X")
                col.prop(vrs, "aspect_y",              text="アスペクト Y")
                col.prop(vrs, "frame_rate",            text="FPS")
            layout.separator()

        # 8) フレーム範囲
        if _fold(layout, sc, "vlm_ui_show_frame_range", "フレーム範囲"):
            vrs = getattr(context.view_layer, "vlm_render", None)
            vlayers = context.scene.view_layers
            is_top_layer = (vlayers and context.view_layer.name == vlayers[0].name)

            if vrs is None:
                layout.label(text="(vlm_render が未登録です)", icon='ERROR')
            else:
                row = layout.row(align=True)
                row.enabled = not is_top_layer
                row.prop(vrs, "frame_enable", text="このレイヤーの値を使用")

                col = layout.column(align=True)
                col.enabled = (is_top_layer or vrs.frame_enable)
                col.prop(sc, "vlm_skip_existing_frames", text="既存フレームをスキップ")
                col.prop(vrs, "frame_start", text="開始フレーム")
                col.prop(vrs, "frame_end",   text="最終フレーム")
                col.prop(vrs, "frame_step",  text="フレームステップ")
            layout.separator()

        # 8.5) シェーダーAOV 自動追加
        row = layout.row(align=True)
        row.label(text="シェーダーAOV", icon='NODE_COMPOSITING')
        op = row.operator("vlm.add_shader_aovs", text="このビューレイヤーに追加")
        op.apply_all = False
        op = row.operator("vlm.add_shader_aovs", text="全ビューレイヤーに追加")
        op.apply_all = True
        layout.separator()

        # 9) 出力ノード（自動）
        if _fold(layout, sc, "vlm_ui_show_output_nodes", "出力ノード（自動）"):
            if hasattr(bpy.types.Scene, "vlm_enable_ao_multiply"):
                layout.prop(sc, "vlm_enable_ao_multiply", text="画像にAOを乗算（RGBカーブ適用）")
            layout.operator("vlm.prepare_output_nodes_plus", text="ノードを作成・接続")
            layout.separator()

        # 10) レンダー出力
        if _fold(layout, sc, "vlm_ui_show_render_output", "レンダー出力"):
            row = layout.row(align=True)
            op = row.operator("vlm.render_all_viewlayers",   text="静止画 (全 Layers)")
            op.use_animation = False
            op = row.operator("vlm.render_active_viewlayer", text="静止画 (アクティブのみ)")
            op.use_animation = False

            row = layout.row(align=True)
            op = row.operator("vlm.render_all_viewlayers",   text="アニメーション (全 Layers)")
            op.use_animation = True
            op = row.operator("vlm.render_active_viewlayer", text="アニメーション (アクティブのみ)")
            op.use_animation = True
            layout.separator()

    # ───────── コレクション再帰描画 ─────────
    def _draw_collections(self, layout, layer_coll, curr, is_top_layer, depth=0):
        def draw_one(lc, d):
            coll = lc.collection
            # ルート（Scene Collection）はスキップして子だけ描画
            if coll.name in {"Scene Collection", "シーンコレクション"}:
                for child in lc.children:
                    draw_one(child, d)
                return

            # ── 1行（1段）にまとめる行コンテナ ─────────────────
            row = layout.row(align=True)
            # 階層のインデント
            row.separator(factor=0.4 + 1 * d)
            # コレクション名
            row.label(text=f"{coll.name}", icon='OUTLINER_COLLECTION')

            # ── トグル（選択可／レンダー／ホールドアウト／間接のみ） ──
            mini = row.row(align=True)

            icon = 'CHECKBOX_HLT' if not lc.exclude else 'CHECKBOX_DEHLT'
            b = mini.operator("vlm.toggle_lc_flag", text="", icon=icon, emboss=True, depress=not lc.exclude)
            b.layer_name = curr; b.collection_name = coll.name; b.flag = "exclude"

            icon = 'RESTRICT_SELECT_OFF' if not coll.hide_select else 'RESTRICT_SELECT_ON'
            b = mini.operator("vlm.toggle_lc_flag", text="", icon=icon, emboss=True, depress=not coll.hide_select)
            b.layer_name = curr; b.collection_name = coll.name; b.flag = "hide_select"

            icon = 'RESTRICT_RENDER_OFF' if not coll.hide_render else 'RESTRICT_RENDER_ON'
            b = mini.operator("vlm.toggle_lc_flag", text="", icon=icon, emboss=True, depress=not coll.hide_render)
            b.layer_name = curr; b.collection_name = coll.name; b.flag = "hide_render"

            icon = 'HOLDOUT_ON' if lc.holdout else 'HOLDOUT_OFF'
            b = mini.operator("vlm.toggle_lc_flag", text="", icon=icon, emboss=True, depress=bool(lc.holdout))
            b.layer_name = curr; b.collection_name = coll.name; b.flag = "holdout"

            icon = 'INDIRECT_ONLY_ON' if lc.indirect_only else 'INDIRECT_ONLY_OFF'
            b = mini.operator("vlm.toggle_lc_flag", text="", icon=icon, emboss=True, depress=bool(lc.indirect_only))
            b.layer_name = curr; b.collection_name = coll.name; b.flag = "indirect_only"

            # ── マテリアル上書き（同じ行の右側へ） ──
            # トグル群との間を少し空ける
            row.separator(factor=0.6)
            mat_row = row.row(align=True)
            mat_row.enabled = not is_top_layer

            key = f"_vlm_mat_override_{curr}_{coll.name}"
            override_mat = coll.get(key)

            if override_mat:
                mat_row.label(text=f"上書: {override_mat}", icon='MATERIAL')
                clr = mat_row.operator("colm.clear_collection_override", text="", icon='X', emboss=True)
                clr.collection_name = coll.name
                clr.layer_name = curr
            else:
                if hasattr(coll, "vlm_temp_mat"):
                    mat_row.prop_search(coll, "vlm_temp_mat", bpy.data, "materials", text="")
                    setop = mat_row.operator("colm.set_collection_override", text="", icon='MATERIAL')
                    setop.collection_name = coll.name
                    setop.layer_name = curr
                    setop.material_name = coll.vlm_temp_mat
                else:
                    mat_row.label(text="一時マテリアル未定義", icon='ERROR')

            # 子コレクションも続けて1段レイアウトで再帰描画
            for child in lc.children:
                draw_one(child, d + 1)

        draw_one(layer_coll, depth)

# ──────────────────────────────────────────────
# register / unregister
# ──────────────────────────────────────────────
def register():
    for cls in (
        VLM_PG_viewlayer_target,
        VLM_PG_collection_multi_state,
        VLM_PG_collection_toggle,
        VLM_PG_render_layer_entry,
        VLM_OT_add_shader_aovs,
        VLM_OT_prepare_output_nodes_plus,
        VLM_OT_duplicate_viewlayers_popup,
        VLM_OT_apply_collection_settings_popup,
        VLM_OT_apply_render_settings_popup,
        VLM_PT_panel,
    ):
        bpy.utils.register_class(cls)

    # Scene プロパティ（チェックボックス）
    bpy.types.Scene.vlm_enable_ao_multiply = bpy.props.BoolProperty(
        name="AO乗算を追加",
        description="“出力ノードを準備” 実行時に、レンダーレイヤーの『画像』出力へAOを乗算（RGBカーブ適用）します",
        default=False
    )

    # WindowManager プロパティ（レンダー設定一括適用用の一時コレクション）
    bpy.types.WindowManager.vlm_render_layers = bpy.props.CollectionProperty(
        type=VLM_PG_render_layer_entry,
    )
    
    if not hasattr(bpy.types.Scene, "vlm_ui_show_world"):
        bpy.types.Scene.vlm_ui_show_world = bpy.props.BoolProperty(
            name="Show World UI", default=False
        )
        
def unregister():
    if hasattr(bpy.types.WindowManager, "vlm_render_layers"):
        del bpy.types.WindowManager.vlm_render_layers

    for cls in (
        VLM_PT_panel,
        VLM_OT_apply_render_settings_popup,
        VLM_OT_apply_collection_settings_popup,
        VLM_OT_duplicate_viewlayers_popup,
        VLM_OT_prepare_output_nodes_plus,
        VLM_OT_add_shader_aovs,
        VLM_PG_render_layer_entry,
        VLM_PG_collection_toggle,
        VLM_PG_collection_multi_state,
        VLM_PG_viewlayer_target,
    ):
        bpy.utils.unregister_class(cls)
    if hasattr(bpy.types.Scene, "vlm_enable_ao_multiply"):
        del bpy.types.Scene.vlm_enable_ao_multiply
    if hasattr(bpy.types.Scene, "vlm_ui_show_world"):
        del bpy.types.Scene.vlm_ui_show_world
