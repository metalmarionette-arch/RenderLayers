import bpy

from . import (
    light_camera          as lc,
    collection_management as colm,
)


class VLM_PG_viewlayer_target(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty(name="ViewLayer Name")
    selected: bpy.props.BoolProperty(name="Selected", default=False)


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
        hint = name_box.box()
        hint.label(text="例: glay→bl として複製すると alp_glay_C1 → alp_bl_C1", icon='INFO')

        layout.separator()
        layout.label(text="複製後にONにするコレクション", icon='OUTLINER_COLLECTION')
        cbox = layout.box()
        for coll in self.collections:
            row = cbox.row(align=True)
            row.separator(factor=0.4 + 0.2 * coll.level)
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
        created = []
        for name in targets:
            src = sc.view_layers.get(name)
            if not src:
                continue
            desired_name = None
            if rename_from:
                replaced = name.replace(rename_from, rename_to)
                desired_name = replaced if replaced else name

            new_vl = colm.duplicate_view_layer_with_collections(
                sc,
                src,
                collection_states=states,
                desired_name=desired_name,
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
            row.separator(factor=0.4 + 0.3 * d)
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
        VLM_PG_collection_toggle,
        VLM_OT_prepare_output_nodes_plus,
        VLM_OT_duplicate_viewlayers_popup,
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
        VLM_OT_duplicate_viewlayers_popup,
        VLM_OT_prepare_output_nodes_plus,
        VLM_PG_collection_toggle,
        VLM_PG_viewlayer_target,
    ):
        bpy.utils.unregister_class(cls)
    if hasattr(bpy.types.Scene, "vlm_enable_ao_multiply"):
        del bpy.types.Scene.vlm_enable_ao_multiply
    if hasattr(bpy.types.Scene, "vlm_ui_show_world"):
        del bpy.types.Scene.vlm_ui_show_world
