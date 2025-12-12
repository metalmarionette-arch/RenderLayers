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

    viewlayers: bpy.props.CollectionProperty(type=VLM_PG_viewlayer_target)

    @staticmethod
    def _camera_poll(_self, obj):
        return bool(obj) and obj.type == 'CAMERA'

    engine_enable: bpy.props.BoolProperty(name="エンジンをこの値で上書き", default=False)
    engine: bpy.props.EnumProperty(name="エンジン", items=ro.ENGINES, default="BLENDER_EEVEE_NEXT")

    samples_enable: bpy.props.BoolProperty(name="サンプル数をこの値で上書き", default=False)
    samples: bpy.props.IntProperty(name="サンプル数", default=64, min=1, max=4096)

    camera_enable: bpy.props.BoolProperty(name="カメラをこの値で上書き", default=False)
    camera: bpy.props.PointerProperty(name="カメラ", type=bpy.types.Object, poll=_camera_poll.__func__)

    world_enable: bpy.props.BoolProperty(name="World をこの値で上書き", default=False)
    world: bpy.props.PointerProperty(name="World", type=bpy.types.World)

    format_enable: bpy.props.BoolProperty(name="フォーマットをこの値で上書き", default=False)
    resolution_x: bpy.props.IntProperty(name="解像度X", default=1920, min=1, max=16384)
    resolution_y: bpy.props.IntProperty(name="解像度Y", default=1080, min=1, max=16384)
    resolution_percentage: bpy.props.IntProperty(name="スケール(%)", default=100, min=1, max=1000)
    aspect_x: bpy.props.FloatProperty(name="アスペクトX", default=1.0, min=0.1)
    aspect_y: bpy.props.FloatProperty(name="アスペクトY", default=1.0, min=0.1)
    frame_rate: bpy.props.FloatProperty(name="FPS", default=24.0, min=0.01, max=60000.0, precision=3)

    frame_enable: bpy.props.BoolProperty(name="フレーム範囲をこの値で上書き", default=False)
    frame_start: bpy.props.IntProperty(name="開始", default=1, min=0)
    frame_end: bpy.props.IntProperty(name="終了", default=250, min=0)
    frame_step: bpy.props.IntProperty(name="ステップ", default=1, min=1)

    def invoke(self, context, event):
        self.viewlayers.clear()
        sc = context.scene
        active = context.view_layer
        active_name = active.name if active else ""

        for vl in sc.view_layers:
            entry = self.viewlayers.add()
            entry.name = vl.name
            entry.selected = (vl.name == active_name)

        if active:
            rs = getattr(active, "vlm_render", None)
            if rs:
                self.engine_enable = bool(getattr(rs, "engine_enable", False))
                self.engine = getattr(rs, "engine", self.engine)

                self.samples_enable = bool(getattr(rs, "samples_enable", False))
                self.samples = getattr(rs, "samples", self.samples)

                self.camera_enable = bool(getattr(rs, "camera_enable", False))
                self.camera = getattr(rs, "camera", None)

                self.world_enable = bool(getattr(rs, "world_enable", False))
                self.world = getattr(active, "vlm_world", None) or sc.world

                self.format_enable = bool(getattr(rs, "format_enable", False))
                self.resolution_x = getattr(rs, "resolution_x", sc.render.resolution_x)
                self.resolution_y = getattr(rs, "resolution_y", sc.render.resolution_y)
                self.resolution_percentage = getattr(rs, "resolution_percentage", sc.render.resolution_percentage)
                self.aspect_x = getattr(rs, "aspect_x", sc.render.pixel_aspect_x)
                self.aspect_y = getattr(rs, "aspect_y", sc.render.pixel_aspect_y)
                self.frame_rate = getattr(rs, "frame_rate", sc.render.fps / sc.render.fps_base)

                self.frame_enable = bool(getattr(rs, "frame_enable", False))
                self.frame_start = getattr(rs, "frame_start", sc.frame_start)
                self.frame_end = getattr(rs, "frame_end", sc.frame_end)
                self.frame_step = getattr(rs, "frame_step", sc.frame_step)

        return context.window_manager.invoke_props_dialog(self, width=520)

    def draw(self, context):
        layout = self.layout
        layout.label(text="適用対象のビューレイヤー", icon='RENDERLAYERS')
        box = layout.box()
        for item in self.viewlayers:
            row = box.row(align=True)
            row.prop(item, "selected", text="")
            row.label(text=item.name, icon='RENDERLAYERS')

        layout.separator()

        layout.label(text="レンダー設定", icon='RENDER_STILL')
        props = layout.box()

        erow = props.row(align=True)
        erow.prop(self, "engine_enable", text="")
        erow.prop(self, "engine", expand=True)

        srow = props.row(align=True)
        srow.prop(self, "samples_enable", text="")
        sval = srow.row(align=True)
        sval.enabled = bool(self.samples_enable)
        sval.prop(self, "samples", text="サンプル数")

        crow = props.row(align=True)
        crow.prop(self, "camera_enable", text="")
        cam_val = crow.row()
        cam_val.enabled = bool(self.camera_enable)
        cam_val.prop(self, "camera", text="カメラ")

        wrow = props.row(align=True)
        wrow.prop(self, "world_enable", text="")
        wval = wrow.row()
        wval.enabled = bool(self.world_enable)
        wval.prop(self, "world", text="World")

        frow = props.row(align=True)
        frow.prop(self, "format_enable", text="")
        fvals = frow.row()
        fvals.enabled = bool(self.format_enable)
        fvals.prop(self, "resolution_x", text="X")
        fvals.prop(self, "resolution_y", text="Y")
        fvals.prop(self, "resolution_percentage", text="スケール")
        fvals = props.row(align=True)
        fvals.enabled = bool(self.format_enable)
        fvals.prop(self, "aspect_x", text="アスペクトX")
        fvals.prop(self, "aspect_y", text="アスペクトY")
        fvals.prop(self, "frame_rate", text="FPS")

        frrow = props.row(align=True)
        frrow.prop(self, "frame_enable", text="")
        frvals = frrow.row()
        frvals.enabled = bool(self.frame_enable)
        frvals.prop(self, "frame_start", text="開始")
        frvals.prop(self, "frame_end", text="終了")
        frvals.prop(self, "frame_step", text="ステップ")

    def execute(self, context):
        sc = context.scene
        targets = [i.name for i in self.viewlayers if i.selected]
        if not targets and context.view_layer:
            targets = [context.view_layer.name]
        if not targets:
            self.report({'WARNING'}, "ビューレイヤーを選択してください")
            return {'CANCELLED'}

        applied = []
        for name in targets:
            vl = sc.view_layers.get(name)
            if vl is None:
                continue
            rs = getattr(vl, "vlm_render", None)
            if rs is None:
                continue

            rs.engine_enable = bool(self.engine_enable)
            rs.engine = self.engine

            rs.samples_enable = bool(self.samples_enable)
            rs.samples = self.samples

            rs.camera_enable = bool(self.camera_enable)
            rs.camera = self.camera if self.camera_enable else rs.camera

            rs.world_enable = bool(self.world_enable)
            if self.world_enable:
                vl.vlm_world = self.world

            rs.format_enable = bool(self.format_enable)
            rs.resolution_x = self.resolution_x
            rs.resolution_y = self.resolution_y
            rs.resolution_percentage = self.resolution_percentage
            rs.aspect_x = self.aspect_x
            rs.aspect_y = self.aspect_y
            rs.frame_rate = self.frame_rate

            rs.frame_enable = bool(self.frame_enable)
            rs.frame_start = self.frame_start
            rs.frame_end = self.frame_end
            rs.frame_step = self.frame_step

            applied.append(name)

        if not applied:
            self.report({'WARNING'}, "適用できるビューレイヤーがありません")
            return {'CANCELLED'}

        try:
            ro.apply_render_override(sc, context.view_layer)
        except Exception:
            pass

        self.report({'INFO'}, f"レンダー設定を適用: {', '.join(applied)}")
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
    # Scene プロパティ（チェックボックス）
    bpy.types.Scene.vlm_enable_ao_multiply = bpy.props.BoolProperty(
        name="AO乗算を追加",
        description="“出力ノードを準備” 実行時に、レンダーレイヤーの『画像』出力へAOを乗算（RGBカーブ適用）します",
        default=False
    )

    for cls in (
        VLM_PG_viewlayer_target,
        VLM_PG_collection_multi_state,
        VLM_PG_collection_toggle,
        VLM_OT_prepare_output_nodes_plus,
        VLM_OT_duplicate_viewlayers_popup,
        VLM_OT_apply_collection_settings_popup,
        VLM_OT_apply_render_settings_popup,
        VLM_PT_panel,
    ):
        bpy.utils.register_class(cls)
    
    if not hasattr(bpy.types.Scene, "vlm_ui_show_world"):
        bpy.types.Scene.vlm_ui_show_world = bpy.props.BoolProperty(
            name="Show World UI", default=False
        )
        
def unregister():
    for cls in (
        VLM_PT_panel,
        VLM_OT_apply_render_settings_popup,
        VLM_OT_apply_collection_settings_popup,
        VLM_OT_duplicate_viewlayers_popup,
        VLM_OT_prepare_output_nodes_plus,
        VLM_PG_collection_toggle,
        VLM_PG_collection_multi_state,
        VLM_PG_viewlayer_target,
    ):
        bpy.utils.unregister_class(cls)
    if hasattr(bpy.types.Scene, "vlm_enable_ao_multiply"):
        del bpy.types.Scene.vlm_enable_ao_multiply
    if hasattr(bpy.types.Scene, "vlm_ui_show_world"):
        del bpy.types.Scene.vlm_ui_show_world