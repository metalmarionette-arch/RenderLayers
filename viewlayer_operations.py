import bpy

# --------------------------------------------------
# LayerCollection 検索・操作ユーティリティ
# --------------------------------------------------
def _find_layer_collection(lc_root, coll_name):
    if lc_root.collection.name == coll_name:
        return lc_root
    for child in lc_root.children:
        res = _find_layer_collection(child, coll_name)
        if res:
            return res
    return None

def _ensure_header_visible(screen):
    """ヘッダー／Info が無いレイアウトなら一時的に Info エリアを作る"""
    import bpy

    # すでに Info エリアがあれば何もしない
    for area in screen.areas:
        if area.type == 'INFO':
            return

    # 最も幅の広いエリアを縦分割して右側を INFO に
    biggest = max(screen.areas, key=lambda a: a.width)

    override = {
        'window': bpy.context.window,
        'screen': screen,
        'area'  : biggest,
        'region': biggest.regions[-1],   # どこでも OK
    }

    # NOTE: 引数は override だけを位置引数にし，他は **キーワード** で渡す
    bpy.ops.screen.area_split(
        override,
        direction='VERTICAL',
        factor=0.83
    )
    # 分割で出来た最後のエリアが右側
    new_area = screen.areas[-1]
    new_area.type = 'INFO'

# --------------------------------------------------
# ビューレイヤー操作オペレーター
# --------------------------------------------------
def _apply_content_collection_overrides(view_layer):
    if not getattr(view_layer, "vlm_content_switch_enable", False):
        return False

    stored_list = getattr(view_layer, "vlm_content_collection_names", [])
    stored_names = {
        item.name for item in stored_list
        if getattr(item, "name", "")
    }

    current_on = set()
    def _gather(lc):
        if lc.collection.name not in {"Scene Collection", "シーンコレクション"}:
            if not lc.exclude:
                current_on.add(lc.collection.name)
        for child in lc.children:
            _gather(child)

    _gather(view_layer.layer_collection)

    if current_on != stored_names:
        stored_list.clear()
        for name in sorted(current_on):
            item = stored_list.add()
            item.name = name
        stored_names = current_on

    def _walk(lc):
        if lc.collection.name not in {"Scene Collection", "シーンコレクション"}:
            lc.exclude = lc.collection.name not in stored_names
        for child in lc.children:
            _walk(child)

    _walk(view_layer.layer_collection)
    return True


class VLM_OT_toggle_collection_in_viewlayer(bpy.types.Operator):
    bl_idname = "vlm.toggle_collection_in_viewlayer"
    bl_label  = "コレクション追加 / 除外"
    bl_options = {'INTERNAL'}

    collection_name : bpy.props.StringProperty()
    layer_name      : bpy.props.StringProperty()
    make_visible    : bpy.props.BoolProperty(default=True)

    def execute(self, context):
        vl = context.scene.view_layers[self.layer_name]
        def _traverse(lc):
            if lc.collection.name == self.collection_name:
                lc.exclude = not self.make_visible
                return True
            return any(_traverse(c) for c in lc.children)
        _traverse(vl.layer_collection)
        return {'FINISHED'}

class VLM_OT_add_empty_viewlayer(bpy.types.Operator):
    bl_idname = "vlm.add_empty_viewlayer"
    bl_label  = "空のビューレイヤー追加"
    bl_options = {'REGISTER', 'UNDO'}

    layer_name : bpy.props.StringProperty(name="ビューレイヤー名", default="NewLayer")

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        sc = context.scene
        name = self.layer_name.strip() or "NewLayer"
        if name in sc.view_layers:
            self.report({'WARNING'}, "同名のビューレイヤーが存在します")
            return {'CANCELLED'}
        vl = sc.view_layers.new(name)
        for c in vl.layer_collection.children:
            c.exclude = True
        self.report({'INFO'}, f"{name} を空で作成しました")
        return {'FINISHED'}

class VLM_OT_set_active_viewlayer(bpy.types.Operator):
    bl_idname = "vlm.set_active_viewlayer"
    bl_label  = "ビューレイヤーをアクティブに"
    bl_options = {'INTERNAL'}

    layer_name : bpy.props.StringProperty()

    def execute(self, context):
        from . import material_override, light_camera
        from .render_override import apply_render_override

        sc = context.scene
        top_vl = sc.view_layers[0]
        dest_vl = sc.view_layers[self.layer_name]

        # ★ トップ→他レイヤーに切り替わる時だけ、事前にバックアップを強制作成
        if context.window.view_layer == top_vl and dest_vl != top_vl:
            try:
                # 既存のオペレーターを呼ぶ：トップを基準にバックアップ作り直し
                bpy.ops.vlm.force_backup_materials()
            except Exception:
                # 万一失敗しても作業を止めない（安全にスルー）
                pass

        # 1) ビューレイヤーを切り替え
        context.window.view_layer = dest_vl

        # 1.5) コレクション内容のON/OFFを反映（必要なレイヤーのみ）
        if _apply_content_collection_overrides(dest_vl):
            try:
                dest_vl.update()
            except Exception:
                pass

        # 2) 各種オーバーライドを適用（従来のまま）
        material_override.apply_active_viewlayer_overrides(context)
        light_camera.apply_lights_for_viewlayer(context.view_layer)
        apply_render_override(context.scene, context.view_layer)

        return {'FINISHED'}


class VLM_OT_remove_viewlayer(bpy.types.Operator):
    bl_idname = "vlm.remove_viewlayer"
    bl_label  = "選択ビューレイヤー削除"
    bl_options = {'REGISTER', 'UNDO'}

    layer_name : bpy.props.StringProperty()

    def execute(self, context):
        from . import material_override

        if self.layer_name == "View Layer":
            if self.layer_name == context.scene.view_layers[0].name and len(context.scene.view_layers) == 1:
                 self.report({'WARNING'}, "最後のビューレイヤーは削除できません")
                 return {'CANCELLED'}

        sc = context.scene
        layer_to_remove = sc.view_layers.get(self.layer_name)
        if not layer_to_remove:
            return {'CANCELLED'}

        was_top_layer = (len(sc.view_layers) > 1 and layer_to_remove == sc.view_layers[0])
        new_top_layer_name = None
        if was_top_layer:
            new_top_layer_name = sc.view_layers[1].name

        sc.view_layers.remove(layer_to_remove)

        if was_top_layer and new_top_layer_name:
            new_top_vl = sc.view_layers.get(new_top_layer_name)
            if new_top_vl:
                for col in bpy.data.collections:
                    key = material_override._override_key(new_top_vl.name, col.name)
                    if key in col:
                        del col[key]
                
                material_override._restore_viewlayer_materials(new_top_vl)
                
                if context.view_layer == new_top_vl:
                    material_override.apply_active_viewlayer_overrides(context)

        return {'FINISHED'}


# 既存: _find_layer_collection(...) などはそのまま

class VLM_OT_toggle_layercollection_flag(bpy.types.Operator):
    """記号ボタンで LayerCollection/Collection の各フラグをトグル"""
    bl_idname = "vlm.toggle_lc_flag"
    bl_label  = "Toggle LC Flag"
    bl_options = {'INTERNAL', 'UNDO'}

    layer_name      : bpy.props.StringProperty()
    collection_name : bpy.props.StringProperty()
    flag            : bpy.props.StringProperty()  # 'exclude' | 'hide_select' | 'hide_render' | 'holdout' | 'indirect_only'

    @classmethod
    def description(cls, context, props):
        # ホバー時の説明
        m = {
            "exclude":       "内容（含める/除外）",
            "hide_select":   "選択可能（選択できる/できない）",
            "hide_render":   "レンダー（出力に含める/含めない）",
            "holdout":       "ホールドアウト（切り抜き）",
            "indirect_only": "間接的のみ（間接光のみ寄与）",
        }
        return m.get(getattr(props, "flag", ""), "切り替え")

    def execute(self, context):
        vl = context.scene.view_layers.get(self.layer_name)
        if not vl:
            return {'CANCELLED'}

        def _find(lc):
            if lc.collection.name == self.collection_name:
                return lc
            for ch in lc.children:
                r = _find(ch)
                if r: return r
            return None

        lc = _find(vl.layer_collection)
        if not lc:
            return {'CANCELLED'}

        coll = lc.collection
        f = self.flag
        if f == "exclude":
            lc.exclude = not lc.exclude
        elif f == "hide_select":
            coll.hide_select = not coll.hide_select
        elif f == "hide_render":
            coll.hide_render = not coll.hide_render
        elif f == "holdout":
            lc.holdout = not lc.holdout
        elif f == "indirect_only":
            lc.indirect_only = not lc.indirect_only

        context.view_layer.update()
        for win in context.window_manager.windows:
            for area in win.screen.areas:
                if area.type in {'VIEW_3D','OUTLINER','PROPERTIES'}:
                    area.tag_redraw()
        return {'FINISHED'}


# --------------------------------------------------
# register / unregister
# --------------------------------------------------
classes = (
    VLM_OT_toggle_collection_in_viewlayer,
    VLM_OT_add_empty_viewlayer,
    VLM_OT_set_active_viewlayer,
    VLM_OT_remove_viewlayer,
    VLM_OT_toggle_layercollection_flag,  # ← これを追加
)
def register():
    for c in classes:
        bpy.utils.register_class(c)

def unregister():
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
