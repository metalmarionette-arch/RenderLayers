# collection_management.py — AO乗算チェーン強化版（Render Layers/Composite 自動補完つき）
import bpy
import os
import re
import time
import pathlib
import datetime
import gc

from . import light_camera
from .material_override import apply_active_viewlayer_overrides
from .render_override import apply_render_override

# collection_management.py の import 群の下あたりに追加
def _get_top_rs(scene):
    try:
        return scene.view_layers[0].vlm_render
    except Exception:
        return None

def _resolve_frame_range(scene, vl):
    """このVLで実際に使うフレーム範囲（start, end, step）を返す。
       VL側がOFFなら『先頭VLのUI値』にフォールバック。"""
    rs     = vl.vlm_render
    top_rs = _get_top_rs(scene) or rs
    if getattr(rs, "frame_enable", False):
        s, e, st = rs.frame_start,  rs.frame_end,  max(1, rs.frame_step)
    else:
        s, e, st = top_rs.frame_start, top_rs.frame_end, max(1, top_rs.frame_step)
    return int(s), int(e), int(st)

def _selected_viewlayers(scene):
    """UI『レンダリングする/しない』チェックを反映して対象VLを選定。
       0件なら vl.use==True、さらに0件なら全VLにフォールバック。"""
    vls = [vl for vl in scene.view_layers if getattr(vl, "vlm_render_this_layer", True)]
    if not vls:
        vls = [vl for vl in scene.view_layers if getattr(vl, "use", True)]
        if not vls:
            vls = list(scene.view_layers)
    return vls


# =========================================================
# ヘルパ
# =========================================================
def _sanitize(name: str) -> str:
    return re.sub(r'[^0-9A-Za-z_\-]', '_', name or "")

def _free_render_images_and_viewers():
    """Render Result/Viewer の参照を外し、孤立データも掃除"""
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'IMAGE_EDITOR':
                for space in area.spaces:
                    if space.type == 'IMAGE_EDITOR':
                        try:
                            img = space.image
                            if img and (img.type in {'RENDER_RESULT', 'COMPOSITING'}):
                                space.image = None
                        except Exception:
                            pass

    for scene in bpy.data.scenes:
        nt = scene.node_tree
        if not nt:
            continue
        for node in nt.nodes:
            if node.type == 'VIEWER':
                try:
                    node.image.user_clear()
                except Exception:
                    pass

    for img in list(bpy.data.images):
        try:
            if img.name.startswith("Viewer Node") or img.name == "Viewer Node":
                bpy.data.images.remove(img)
        except Exception:
            pass

    try:
        bpy.data.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)
    except TypeError:
        bpy.data.orphans_purge()

def _vlm_purge(scene):
    if scene.render.engine == 'BLENDER_EEVEE_NEXT':
        try:
            scene.render.engine = 'BLENDER_WORKBENCH'
            scene.render.engine = 'BLENDER_EEVEE_NEXT'
        except Exception:
            pass
    _free_render_images_and_viewers()
    gc.collect()
    try:
        bpy.data.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)
    except TypeError:
        bpy.data.orphans_purge()

def _defer_strong_purge(scene, delay=0.0):
    def _run():
        try:
            _vlm_purge(scene)
        except Exception:
            pass
        return None
    try:
        bpy.app.timers.register(_run, first_interval=float(delay))
    except Exception:
        pass


def _unique_view_layer_name(scene, base):
    name = base
    i = 1
    while name in scene.view_layers:
        name = f"{base}.{i:03d}"
        i += 1
    return name


def _apply_collection_states_to_viewlayer(vl, collection_states):
    def _walk(lc):
        desired = collection_states.get(lc.collection.name)
        if desired is not None:
            lc.exclude = not bool(desired)
        for child in lc.children:
            _walk(child)
    _walk(vl.layer_collection)


def duplicate_view_layer_with_collections(scene, source_vl, *, collection_states=None):
    """アクティブを一時切替えてコピーし、コレクションON/OFFを即適用"""
    collection_states = collection_states or {}
    win = bpy.context.window
    orig_vl = win.view_layer
    new_vl = None
    try:
        win.view_layer = source_vl
        bpy.ops.scene.view_layer_add(type='COPY')
        new_vl = win.view_layer
        new_vl.name = _unique_view_layer_name(scene, f"{source_vl.name}_copy")
        if collection_states:
            _apply_collection_states_to_viewlayer(new_vl, collection_states)
    except Exception:
        new_vl = None
    finally:
        try:
            win.view_layer = orig_vl
        except Exception:
            pass
    return new_vl

# =========================================================
# Collection × ViewLayer のマテリアル上書き（2オペ）
# =========================================================
def _override_key(layer_name: str, collection_name: str) -> str:
    return f"_vlm_mat_override_{layer_name}_{collection_name}"

class COLM_OT_set_collection_override(bpy.types.Operator):
    bl_idname = "colm.set_collection_override"
    bl_label  = "コレクションにマテリアル上書きを設定"
    bl_options = {'UNDO'}

    collection_name : bpy.props.StringProperty()
    layer_name      : bpy.props.StringProperty()
    material_name   : bpy.props.StringProperty()

    def execute(self, context):
        coll = bpy.data.collections.get(self.collection_name)
        if coll is None:
            self.report({'ERROR'}, f"Collection not found: {self.collection_name}")
            return {'CANCELLED'}

        vl = context.scene.view_layers.get(self.layer_name)
        if vl is None:
            self.report({'ERROR'}, f"ViewLayer not found: {self.layer_name}")
            return {'CANCELLED'}

        mat = bpy.data.materials.get(self.material_name) if self.material_name else None
        if mat is None:
            self.report({'ERROR'}, f"Material not found: {self.material_name}")
            return {'CANCELLED'}

        key = _override_key(self.layer_name, self.collection_name)
        coll[key] = mat.name  # 名前で保存

        try:
            apply_active_viewlayer_overrides(context)
        except Exception:
            pass

        self.report({'INFO'}, f"Override set: {self.collection_name} -> {mat.name} (VL:{self.layer_name})")
        return {'FINISHED'}


class COLM_OT_clear_collection_override(bpy.types.Operator):
    bl_idname = "colm.clear_collection_override"
    bl_label  = "コレクションのマテリアル上書きを解除"
    bl_options = {'UNDO'}

    collection_name : bpy.props.StringProperty()
    layer_name      : bpy.props.StringProperty()

    def execute(self, context):
        coll = bpy.data.collections.get(self.collection_name)
        if coll is None:
            self.report({'ERROR'}, f"Collection not found: {self.collection_name}")
            return {'CANCELLED'}

        vl = context.scene.view_layers.get(self.layer_name)
        if vl is None:
            self.report({'ERROR'}, f"ViewLayer not found: {self.layer_name}")
            return {'CANCELLED'}

        key = _override_key(self.layer_name, self.collection_name)
        if key in coll:
            try:
                del coll[key]
            except Exception:
                coll[key] = ""

        try:
            apply_active_viewlayer_overrides(context)
        except Exception:
            pass

        self.report({'INFO'}, f"Override cleared: {self.collection_name} (VL:{self.layer_name})")
        return {'FINISHED'}


# =========================================================
# 出力ノード準備（File Output 最低一個）＋ 動的パス更新
# =========================================================
def _ensure_compositor_and_min_nodes(sc: bpy.types.Scene):
    """Compositor ON、Render Layers/Composite を最低限確保しておく"""
    if not sc.use_nodes:
        sc.use_nodes = True
    nt = sc.node_tree
    if nt is None:
        return None

    # Composite ノード
    comp = next((n for n in nt.nodes if n.type == 'COMPOSITE'), None)
    if comp is None:
        comp = nt.nodes.new("CompositorNodeComposite")
        comp.location = (800, 200)

    # Render Layers ノード（無ければアクティブVL用を作成）
    rlayers = [n for n in nt.nodes if n.type == 'R_LAYERS']
    if not rlayers:
        rl = nt.nodes.new("CompositorNodeRLayers")
        rl.layer = bpy.context.view_layer.name if bpy.context.view_layer else sc.view_layers[0].name
        rl.location = (200, 200)
        rlayers = [rl]

        # 最低限の配線（RLayer Image → Composite）
        try:
            nt.links.new(rl.outputs.get("Image"), comp.inputs[0])
        except Exception:
            pass

    return nt

# 追加ヘルパー：優先File Outputの取得と、空き入力ソケットの取得
def _find_preferred_file_output(nt: bpy.types.NodeTree):
    """VLM_AutoOutput を最優先で返し、無ければ最初の OUTPUT_FILE を返す。"""
    fos = [n for n in nt.nodes if n.type == 'OUTPUT_FILE']
    if not fos:
        return None
    for n in fos:
        if getattr(n, "label", "") == "VLM_AutoOutput" or n.name == "VLM_AutoOutput":
            return n
    return fos[0]

def _ensure_free_input_on_file_output(fo: bpy.types.Node):
    """File Output に未使用入力を1つ保証して返す。無ければスロット作成。"""
    for sock in fo.inputs:
        if not sock.is_linked:
            return sock
    # すべて埋まっている → 新規スロット
    try:
        fo.file_slots.new("VLM_AO")
        return fo.inputs[-1]
    except Exception:
        # どうしてもダメなら最初の入力へ
        return fo.inputs[0]

def _get_free_file_output_input(fo: bpy.types.NodeSocket):
    """File Output の未使用入力を返す。無ければスロットを新規作成して返す。"""
    for sock in fo.inputs:
        if not sock.is_linked:
            return sock
    # すべて埋まっている場合は新規スロットを作成
    try:
        fo.file_slots.new("VLM_AO")
        return fo.inputs[-1]
    except Exception:
        # それでも失敗したら最初の入力に上書きで刺す
        return fo.inputs[0]


def _ensure_file_output(nt: bpy.types.NodeTree):
    """File Output が一つも無ければ VLM_AutoOutput を作る（既存があれば触らない）"""
    out = next((n for n in nt.nodes if n.type == 'OUTPUT_FILE'), None)
    if out is None:
        out = nt.nodes.new("CompositorNodeOutputFile")
        out.label = "VLM_AutoOutput"
        out.base_path = "//render_out"
        out.format.file_format = 'PNG'
        out.location = (1000, 200)

        # 既存 Composite 経由の画像があれば、それをFile Outputにも分岐
        comp = next((n for n in nt.nodes if n.type == 'COMPOSITE'), None)
        if comp and comp.inputs and comp.inputs[0].is_linked:
            try:
                from_sock = comp.inputs[0].links[0].from_socket
                nt.links.new(from_sock, out.inputs[0])
            except Exception:
                pass

# （collection_management.py 内の適切な位置に追加）

def _ensure_viewlayer_output_node(scene, nt, vl_name, *, blend_name, base_fp, img_set, index=0):
    """ViewLayerごとのFile Outputノードを1つだけ用意し、見つからなければ作成して返す"""
    # 既存の管理ノードを探す（vl_nameで一致）
    for n in nt.nodes:
        if n.type == 'OUTPUT_FILE' and n.get("vlm_managed") and n.get("vl_name") == vl_name:
            # フォーマットを現在のシーン設定に追従
            n.format.file_format = img_set.file_format
            n.format.color_mode  = img_set.color_mode
            n.format.color_depth = img_set.color_depth
            if hasattr(img_set, "compression"):
                n.format.compression = img_set.compression
            if hasattr(img_set, "exr_codec"):
                n.format.exr_codec   = img_set.exr_codec
            return n

    # なければ新規作成
    fo = nt.nodes.new("CompositorNodeOutputFile")
    fo.label = f"VLM_OUT_{vl_name}"
    fo.location = (600, -360 * index)
    fo["vlm_managed"] = True
    fo["vl_name"] = vl_name  # ← 1対1紐付けのキー

    # 基本フォルダ（blend名 / VL名 まで）… パス名は _update_dynamic_file_output_paths で都度更新
    def sanitize(name):
        import re
        return re.sub(r'[^0-9A-Za-z_\-]', '_', name)

    base_dir = os.path.join(base_fp, sanitize(blend_name), sanitize(vl_name)) + os.sep
    fo.base_path = base_dir

    # 出力フォーマットをシーン設定に合わせる
    fo.format.file_format = img_set.file_format
    fo.format.color_mode  = img_set.color_mode
    fo.format.color_depth = img_set.color_depth
    if hasattr(img_set, "compression"):
        fo.format.compression = img_set.compression
    if hasattr(img_set, "exr_codec"):
        fo.format.exr_codec   = img_set.exr_codec

    # 最低1スロット確保（Image 用のダミー）
    if not fo.file_slots:
        fo.file_slots.new("Image")  # pathは後で更新
    return fo


def _connect_pass_to_slot(nt, rl_node, fo_node, pass_name, *, blend_name, vl_name):
    """
    指定パス名（例: 'Image', 'Z', 'Mist', 'MyAOV'）を、fo_nodeのスロットに接続する。
    すでに同じ Render Layers 出力からのリンクがあれば何もしない。
    無ければスロットを新設し、最後尾の入力へ接続する。
    """
    sock = rl_node.outputs.get(pass_name)
    if not sock:
        return  # そのパスが無い

    # 既存リンクの重複チェック（このVLのこのパスが既に刺さっていないか）
    for i, inp in enumerate(fo_node.inputs):
        if inp.is_linked:
            lk = inp.links[0]
            if lk.from_socket == sock:
                return  # もう繋がっている

    # 新しいスロット（＝入力）を作る
    slot = fo_node.file_slots.new(pass_name)

    # スロットに対するファイル名パターン（同ノード内は base_path 共通／slot.path で振り分け）
    safe_vl = vl_name
    safe_pass = pass_name.replace(" ", "_")
    slot.path = f"{blend_name}_{safe_vl}_{safe_pass}_"

    # 最後尾の入力に接続
    inp = fo_node.inputs[-1]
    try:
        nt.links.new(sock, inp)
    except Exception:
        pass


# 置換：既存 _update_dynamic_file_output_paths(scene)
def _update_dynamic_file_output_paths(scene):
    """アドオン管理の File Output ノード（vlm_managed=True）だけを最新設定に更新"""
    if not scene.use_nodes or not scene.node_tree:
        return
    import os, re, pathlib
    nt = scene.node_tree
    blend_name = pathlib.Path(bpy.data.filepath).stem or "Untitled"
    base_fp = scene.render.filepath

    def sanitize(s): return re.sub(r'[^0-9A-Za-z_\-]', '_', s)

    for n in nt.nodes:
        if n.type != 'OUTPUT_FILE' or not n.get("vlm_managed"):
            continue
        vl = n.get("vl_name") or "ViewLayer"
        ps = n.get("pass_name") or "Image"
        n.base_path = os.path.join(base_fp, sanitize(blend_name), sanitize(vl), sanitize(ps)) + os.sep
        # スロット（常に1つ）を再設定
        if not n.file_slots:
            n.file_slots.new(ps)
        while len(n.file_slots) > 1:
            n.file_slots.remove(n.file_slots[-1])
        n.file_slots[0].path = f"{sanitize(blend_name)}_{sanitize(vl)}_{sanitize(ps)}_"

# =========================================================
# AO 乗算チェーン（チェックON時のみ）
#   Render Layers の Image 出力の旧宛先を退避 → Image×(RGBカーブ(AO)) を挿入して再接続
#   AO ソケットは "AO" / "Ambient Occlusion" / "Occlusion" など名前揺れに対応
# =========================================================
def _find_ao_socket(rl_node: bpy.types.Node):
    # まず "AO" を試す
    sock = rl_node.outputs.get("AO")
    if sock:
        return sock
    # 次に名前揺れ対応
    keynames = ("ambient occlusion", "occlusion", "ao")
    for s in rl_node.outputs:
        nm = s.name.strip().lower()
        if any(k in nm for k in keynames):
            return s
    return None

# 置き換え：AO乗算チェーン本体（2点目を (0.64, 0.86) に変更＆出力へ確実に接続）
def _apply_ao_multiply_chain(sc: bpy.types.Scene):
    if not getattr(sc, "vlm_enable_ao_multiply", False):
        return
    nt = sc.node_tree
    if not sc.use_nodes or nt is None:
        return

    # Render Layers / Composite を最低限用意
    _ensure_compositor_and_min_nodes(sc)

    rlayers = [n for n in nt.nodes if n.type == 'R_LAYERS']
    if not rlayers:
        return

    for rl in rlayers:
        # 対応 ViewLayer の AO パス ON
        vl_name = getattr(rl, "layer", None) or (bpy.context.view_layer.name if bpy.context.view_layer else None)
        vl = sc.view_layers.get(vl_name) if vl_name else None
        if vl and hasattr(vl, "use_pass_ambient_occlusion") and not vl.use_pass_ambient_occlusion:
            vl.use_pass_ambient_occlusion = True

        sock_img = rl.outputs.get("Image")

        # AO ソケット（名称ゆれ対応）
        sock_ao = rl.outputs.get("AO")
        if not sock_ao:
            for s in rl.outputs:
                nm = s.name.strip().lower()
                if any(k in nm for k in ("ambient occlusion", "occlusion", "ao")):
                    sock_ao = s
                    break

        if sock_img is None or sock_ao is None:
            continue

        # 二重適用防止
        mix_tag = f"VLM_AO_Mix__{vl_name or rl.name}"
        if next((n for n in nt.nodes if n.type == 'MIX_RGB' and getattr(n, "label", "") == mix_tag), None):
            # 既にMixがある場合でも、File Output に繋がっていなければ繋ぐ
            mix = next((n for n in nt.nodes if n.type == 'MIX_RGB' and getattr(n, "label", "") == mix_tag), None)
            if mix:
                out_sock = mix.outputs.get("Image", mix.outputs[0])
                fo = _find_preferred_file_output(nt)
                if fo is None:
                    fo = nt.nodes.new("CompositorNodeOutputFile")
                    fo.label = "VLM_AutoOutput"
                    fo.base_path = "//render_out"
                    fo.format.file_format = 'PNG'
                    fo.location = (mix.location.x + 260, mix.location.y)
                # すでにMixからFOへリンクがあるか？
                already = any(l.from_socket == out_sock for inp in fo.inputs for l in inp.links)
                if not already:
                    sock_in = _ensure_free_input_on_file_output(fo)
                    try:
                        nt.links.new(out_sock, sock_in)
                    except Exception:
                        comp = next((n for n in nt.nodes if n.type == 'COMPOSITE'), None)
                        if comp:
                            try:
                                nt.links.new(out_sock, comp.inputs[0])
                            except Exception:
                                pass
            continue

        # 既存 Image 出力のリンクを退避 → 切断
        old_links = list(sock_img.links)
        for link in old_links:
            try:
                nt.links.remove(link)
            except Exception:
                pass

        # MixRGB（乗算）と RGB Curves を作成
        mix = nt.nodes.new("CompositorNodeMixRGB")
        mix.blend_type = 'MULTIPLY'
        mix.inputs[0].default_value = 1.0  # Fac=1
        mix.label = mix_tag
        mix["vlm_ao_multiply"] = True
        mix.location = (rl.location.x + 220, rl.location.y - 40)

        curves = nt.nodes.new("CompositorNodeCurveRGB")
        curves.label = f"VLM_AO_Curves__{vl_name or rl.name}"
        curves["vlm_ao_multiply"] = True
        curves.location = (rl.location.x + 220, rl.location.y - 180)

        # 接続： AO → Curves → Mix(B)、Image → Mix(A)
        nt.links.new(sock_ao, curves.inputs.get("Image", curves.inputs[0]))
        nt.links.new(sock_img, mix.inputs.get("Color1", mix.inputs[1]))
        nt.links.new(curves.outputs.get("Image", curves.outputs[0]), mix.inputs.get("Color2", mix.inputs[2]))

        # RGBカーブ（Combined）に 2点追加： (0.2,0.2), (0.64,0.86)
        mp = curves.mapping
        mp.use_clip = False
        try:
            ccurve = mp.curves[3]  # Combined
        except Exception:
            ccurve = mp.curves[-1]

        def _has_point(x, y, eps=1e-4):
            return any(abs(p.location[0]-x) < eps and abs(p.location[1]-y) < eps for p in ccurve.points)

        if not _has_point(0.2, 0.2):
            ccurve.points.new(0.2, 0.2)
        if not _has_point(0.64, 0.86):   # ★ ご指定どおり 0.86
            ccurve.points.new(0.64, 0.86)
        mp.update()

        # Mix の出力を旧宛先へ再接続
        out_sock = mix.outputs.get("Image", mix.outputs[0])
        for link in old_links:
            try:
                nt.links.new(out_sock, link.to_socket)
            except Exception:
                pass

        # さらに必ず File Output にも分岐接続（無ければ作成／満杯なら増設）
        fo = _find_preferred_file_output(nt)
        if fo is None:
            fo = nt.nodes.new("CompositorNodeOutputFile")
            fo.label = "VLM_AutoOutput"
            fo.base_path = "//render_out"
            fo.format.file_format = 'PNG'
            fo.location = (mix.location.x + 260, mix.location.y)
        # 既にMix→FOがあるか？
        already = any(l.from_socket == out_sock for inp in fo.inputs for l in inp.links)
        if not already:
            sock_in = _ensure_free_input_on_file_output(fo)
            try:
                nt.links.new(out_sock, sock_in)
            except Exception:
                # 最終フォールバック：Composite へ
                comp = next((n for n in nt.nodes if n.type == 'COMPOSITE'), None)
                if comp:
                    try:
                        nt.links.new(out_sock, comp.inputs[0])
                    except Exception:
                        pass
# 追加：全 ViewLayer 分の Render Layers ノードを用意
def _ensure_rlayers_for_all_viewlayers(sc: bpy.types.Scene, nt: bpy.types.NodeTree):
    existing = {getattr(n, "layer", None): n for n in nt.nodes if n.type == 'R_LAYERS'}
    y0, dy = 200, 220  # 並べる位置（重なり回避）
    created = []
    for i, vl in enumerate(sc.view_layers):
        if vl.name in existing:
            continue
        rl = nt.nodes.new("CompositorNodeRLayers")
        rl.layer = vl.name
        rl.location = (200, y0 - i*dy)
        created.append(rl)
    # 既存＋新規すべて返す
    return [n for n in nt.nodes if n.type == 'R_LAYERS']

# 追加：AOオフ時でも各 RLayer → File Output へ直結しておく
def _link_rlayers_direct_to_file_output(sc: bpy.types.Scene, nt: bpy.types.NodeTree):
    fo = _find_preferred_file_output(nt)
    if fo is None:
        fo = nt.nodes.new("CompositorNodeOutputFile")
        fo.label = "VLM_AutoOutput"
        fo.base_path = "//render_out"
        fo.format.file_format = 'PNG'
        fo.location = (1000, 200)

    for rl in [n for n in nt.nodes if n.type == 'R_LAYERS']:
        sock_img = rl.outputs.get("Image")
        if sock_img is None:
            continue
        # すでにどこかへ繋がっていればスキップ（AOオン時は後で再配線されます）
        if any(l for l in sock_img.links):
            continue
        sock_in = _ensure_free_input_on_file_output(fo)
        try:
            nt.links.new(sock_img, sock_in)
        except Exception:
            pass

            
# 追加（必要ならファイル冒頭に imports が無ければ関数内で import しています）
def _ensure_output_node_for_pass(scene, nt, vl_name, pass_name, *, blend_name, base_fp, img_set, x=900, y=0):
    """
    ViewLayer名とパス名に対応する File Output ノードをちょうど1つだけ用意し、返す。
    既存があれば再利用。base_path とファイル設定を同期する。
    """
    import os, re
    def sanitize(s): return re.sub(r'[^0-9A-Za-z_\-]', '_', s)

    # 既存を探索（カスタムプロパティで管理）
    for n in nt.nodes:
        if n.type == 'OUTPUT_FILE' and n.get("vlm_managed") and n.get("vl_name")==vl_name and n.get("pass_name")==pass_name:
            # 出力設定を同期
            n.format.file_format = img_set.file_format
            n.format.color_mode  = img_set.color_mode
            n.format.color_depth = img_set.color_depth
            if hasattr(img_set, "compression"):
                n.format.compression = img_set.compression
            if hasattr(img_set, "exr_codec"):
                n.format.exr_codec   = img_set.exr_codec
            # base_path 更新（blend / VL / PASS）
            n.base_path = os.path.join(base_fp, sanitize(blend_name), sanitize(vl_name), sanitize(pass_name)) + os.sep
            # スロットは常に1つだけに揃える
            while len(n.file_slots) > 1:
                n.file_slots.remove(n.file_slots[-1])
            if not n.file_slots:
                n.file_slots.new(pass_name)
            n.file_slots[0].path = f"{sanitize(blend_name)}_{sanitize(vl_name)}_{sanitize(pass_name)}_"
            return n

    # 無ければ新規作成
    fo = nt.nodes.new("CompositorNodeOutputFile")
    fo.label = f"VLM_OUT_{vl_name}_{pass_name}"
    fo.location = (x, y)
    fo["vlm_managed"] = True
    fo["vl_name"]   = vl_name
    fo["pass_name"] = pass_name

    fo.format.file_format = img_set.file_format
    fo.format.color_mode  = img_set.color_mode
    fo.format.color_depth = img_set.color_depth
    if hasattr(img_set, "compression"):
        fo.format.compression = img_set.compression
    if hasattr(img_set, "exr_codec"):
        fo.format.exr_codec   = img_set.exr_codec

    fo.base_path = os.path.join(base_fp, sanitize(blend_name), sanitize(vl_name), sanitize(pass_name)) + os.sep

    # スロットは1つに固定
    if not fo.file_slots:
        fo.file_slots.new(pass_name)
    else:
        fo.file_slots[0].path = ""
    fo.file_slots[0].path = f"{sanitize(blend_name)}_{sanitize(vl_name)}_{sanitize(pass_name)}_"
    return fo

# =========================================================
# 出力ノードの準備（オペレーター）
# =========================================================
# 既存の _prepare_compositor_nodes(scene) をこの実装で置換

# 置換：既存 _prepare_compositor_nodes(scene)
def _prepare_compositor_nodes(scene):
    """
    各 ViewLayer について：
      - Render Layers ノードを用意
      - その ViewLayer に「存在する/有効な」各パス（Image/Z/Mist/…/AOV）ごとに
        File Output ノードを1つ作り、該当出力をそのノードの唯一の入力に接続
    """
    import pathlib, bpy

    # コンポジターON
    if not scene.use_nodes:
        scene.use_nodes = True
    nt = scene.node_tree
    if nt is None:
        return

    # 既存 R_LAYERS をマップ化
    rl_map = {n.layer: n for n in nt.nodes if isinstance(n, bpy.types.CompositorNodeRLayers)}

    blend_name = pathlib.Path(bpy.data.filepath).stem or "Untitled"
    base_fp = scene.render.filepath
    img_set = scene.render.image_settings

    # 名称→有効フラグの簡易マップ（存在チェックも別途行う）
    def is_enabled(vl, name):
        # Image は常に
        if name == "Image": return True
        # 深度/霧/法線/ベクトル
        if name == "Z":     return bool(getattr(vl, "use_pass_z", False))
        if name == "Mist":  return bool(getattr(vl, "use_pass_mist", False))
        if name == "Normal":return bool(getattr(vl, "use_pass_normal", False))
        if name == "Vector":return bool(getattr(vl, "use_pass_vector", False))
        if name == "Position": return bool(getattr(vl, "use_pass_position", False))
        # ライティング系（ソケット名はBlender表記に合わせる）
        if name == "Diffuse Direct":  return bool(getattr(vl, "use_pass_diffuse_direct", False))
        if name == "Diffuse Color":   return bool(getattr(vl, "use_pass_diffuse_color", False))
        if name == "Glossy Direct":   return bool(getattr(vl, "use_pass_glossy_direct", False))
        if name == "Glossy Color":    return bool(getattr(vl, "use_pass_glossy_color", False))
        if name == "Emit" or name == "Emission": return bool(getattr(vl, "use_pass_emit", False))
        if name == "Environment":     return bool(getattr(vl, "use_pass_environment", False))
        if name == "Shadow":          return bool(getattr(vl, "use_pass_shadow", False))
        if name == "Ambient Occlusion": return bool(getattr(vl, "use_pass_ambient_occlusion", False))
        # それ以外（AOVなど）は、ソケットがある＝有効とみなす
        return True

    # 各 ViewLayer を処理
    for i, vl in enumerate(scene.view_layers):
        # Render Layers ノード
        if vl.name in rl_map:
            rl = rl_map[vl.name]
        else:
            rl = nt.nodes.new("CompositorNodeRLayers")
            rl.layer = vl.name
            rl.location = (200, -400 * i)
            rl_map[vl.name] = rl

        # このRLに存在する出力名を収集
        socket_names = [s.name for s in rl.outputs]

        # 既知の標準パス候補
        standard_names = [
            "Image", "Z", "Mist", "Normal", "Vector", "Position",
            "Diffuse Direct", "Diffuse Color",
            "Glossy Direct", "Glossy Color",
            "Emit", "Emission", "Environment", "Shadow", "Ambient Occlusion",
        ]
        # RLに実在するものだけに絞り込む（さらに有効フラグで選別）
        active_names = [n for n in standard_names if (n in socket_names and is_enabled(vl, n))]

        # AOV は ViewLayer 側の定義から取り、RLに出力があるものだけ
        aov_names = []
        if hasattr(vl, "aovs"):
            for aov in vl.aovs:
                if aov and aov.name and (aov.name in socket_names):
                    aov_names.append(aov.name)

        # 位置の見やすさ（縦並び）
        y0, dy = -100, -180
        # 標準パス
        for j, name in enumerate(active_names):
            sock = rl.outputs.get(name)
            if not sock:
                continue
            fo = _ensure_output_node_for_pass(scene, nt, vl.name, name,
                                              blend_name=blend_name, base_fp=base_fp, img_set=img_set,
                                              x=900, y=(-400 * i) + y0 - dy * j)
            # 入力は常に1つ：既存リンクが別ソースなら外して差し替え
            inp = fo.inputs[0]
            if inp.is_linked:
                lk = inp.links[0]
                if lk.from_socket != sock:
                    nt.links.remove(lk)
            if not inp.is_linked:
                nt.links.new(sock, inp)

        # AOVパス
        start = len(active_names)
        for k, name in enumerate(aov_names):
            sock = rl.outputs.get(name)
            if not sock:
                continue
            fo = _ensure_output_node_for_pass(scene, nt, vl.name, name,
                                              blend_name=blend_name, base_fp=base_fp, img_set=img_set,
                                              x=1200, y=(-400 * i) + y0 - dy * (start + k))
            inp = fo.inputs[0]
            if inp.is_linked:
                lk = inp.links[0]
                if lk.from_socket != sock:
                    nt.links.remove(lk)
            if not inp.is_linked:
                nt.links.new(sock, inp)

    # 仕上げ：念のためパス更新
    _update_dynamic_file_output_paths(scene)


def _update_dynamic_paths_and_apply_ao(sc: bpy.types.Scene):
    _update_dynamic_file_output_paths(sc)
    _apply_ao_multiply_chain(sc)

class VLM_OT_prepare_output_nodes(bpy.types.Operator):
    bl_idname  = "vlm.prepare_output_nodes"
    bl_label   = "出力ノードを準備"
    bl_options = {'REGISTER'}

    def execute(self, context):
        sc = context.scene
        _prepare_compositor_nodes(sc)
        _update_dynamic_paths_and_apply_ao(sc)
        self.report({'INFO'}, "出力ノードを作成・接続しました")
        return {'FINISHED'}

# =========================================================
# （参考）レンダー関連：アクティブ／全レイヤー
# =========================================================
class VLM_OT_render_active_viewlayer(bpy.types.Operator):
    bl_idname  = "vlm.render_active_viewlayer"
    bl_label   = "アクティブなビューレイヤーをレンダリング"
    bl_options = {'REGISTER'}

    use_animation: bpy.props.BoolProperty(name="アニメーション", default=False)

    def execute(self, context):
        sc  = context.scene
        win = context.window

        orig_vl     = win.view_layer
        orig_engine = sc.render.engine
        orig_frame  = sc.frame_current
        orig_world  = sc.world

        try:
            # アクティブのみON
            target_vl = None
            for vl in sc.view_layers:
                is_tgt = (vl == win.view_layer)
                vl.use = is_tgt
                if is_tgt:
                    target_vl = vl
            if target_vl:
                win.view_layer = target_vl
            vl = win.view_layer

            if not self.use_animation:
                apply_active_viewlayer_overrides(context)
                light_camera.apply_lights_for_viewlayer(vl)
                apply_render_override(sc, vl)
                sc.frame_set(sc.frame_current)
                _prepare_compositor_nodes(sc)
                _update_dynamic_paths_and_apply_ao(sc)
                bpy.ops.render.render(write_still=True, use_viewport=False)
                _vlm_purge(sc); _free_render_images_and_viewers(); _defer_strong_purge(sc, delay=0.1)
            else:
                # ★ 実レンジを解決（OFFなら先頭VLのUI値）
                start, end, step = _resolve_frame_range(sc, vl)

                total = ((end - start) // step) + 1
                context.window_manager.progress_begin(0, total)
                done = 0

                f = start
                while f <= end:
                    apply_active_viewlayer_overrides(context)
                    light_camera.apply_lights_for_viewlayer(vl)
                    apply_render_override(sc, vl)

                    sc.frame_set(f)
                    _prepare_compositor_nodes(sc)
                    _update_dynamic_paths_and_apply_ao(sc)
                    bpy.ops.render.render(write_still=True, use_viewport=False)
                    _vlm_purge(sc); _free_render_images_and_viewers(); _defer_strong_purge(sc, delay=0.1)

                    done += 1
                    context.window_manager.progress_update(done)
                    f += step

                context.window_manager.progress_end()

        finally:
            # 復元
            try:
                for v in sc.view_layers:
                    v.use = (v == orig_vl)
                win.view_layer   = orig_vl
                sc.render.engine = orig_engine
                sc.frame_set(orig_frame)
                sc.world         = orig_world
            except Exception:
                pass

        self.report({'INFO'}, "レンダリングが完了しました")
        return {'FINISHED'}


class VLM_OT_render_all_viewlayers(bpy.types.Operator):
    bl_idname = "vlm.render_all_viewlayers"
    bl_label  = "全レイヤーをレンダリング (キャンセル可)"
    bl_options = {'REGISTER'}

    use_animation: bpy.props.BoolProperty(name="アニメーション", default=False)

    def invoke(self, context, event):
        sc = context.scene
        wm = context.window_manager

        # UIのチェックに基づいて対象を決定
        self._vl_list = _selected_viewlayers(sc)
        if not self._vl_list:
            self.report({'ERROR'}, "レンダリング対象がありません")
            return {'CANCELLED'}

        # 総ステップを“各VLの実際に使うレンジ”で計算
        if self.use_animation:
            self._total_steps = 0
            for vl in self._vl_list:
                s, e, st = _resolve_frame_range(sc, vl)
                self._total_steps += ((e - s) // st) + 1
        else:
            self._total_steps = len(self._vl_list)

        self._vl_index = 0
        # ★ 初期フレームを“最初のVLの解決レンジstart”にセット
        first_start, _, _ = _resolve_frame_range(sc, self._vl_list[0])
        self._frame = first_start
        # ★ 次VLに切替えた直後にレンジstartへリセットするフラグ
        self._need_reset_frame = False

        self._done_steps = 0
        wm.progress_begin(0, self._total_steps)
        self._timer = wm.event_timer_add(0.01, window=context.window)
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type == 'ESC':
            context.window_manager.progress_end()
            self.report({'WARNING'}, "キャンセルしました")
            return {'CANCELLED'}

        if event.type != 'TIMER':
            return {'PASS_THROUGH'}

        sc  = context.scene
        win = context.window
        wm  = context.window_manager

        # 全部終わった
        if self._vl_index >= len(self._vl_list):
            wm.progress_end()
            self.report({'INFO'}, "全レイヤーのレンダリングが完了しました。")
            return {'FINISHED'}

        vl = self._vl_list[self._vl_index]

        # このVLだけ有効＆アクティブ
        for v in sc.view_layers:
            v.use = (v == vl)
        win.view_layer = vl

        # ★ このVLで実際に使うレンジ
        start, end, step = _resolve_frame_range(sc, vl)

        # ★ レイヤー切替直後 or 範囲外なら、必ず start から回す
        if self._need_reset_frame or (self._frame < start or self._frame > end):
            self._frame = start
            self._need_reset_frame = False

        # 各種オーバーライド適用
        apply_active_viewlayer_overrides(context)
        light_camera.apply_lights_for_viewlayer(vl)
        apply_render_override(sc, vl)

        # レンダリング実行
        sc.frame_set(self._frame)
        _prepare_compositor_nodes(sc)
        _update_dynamic_paths_and_apply_ao(sc)
        bpy.ops.render.render(write_still=True, use_viewport=False)
        _vlm_purge(sc); _free_render_images_and_viewers(); _defer_strong_purge(sc, delay=0.1)

        # 進捗
        self._done_steps += 1
        wm.progress_update(self._done_steps)

        if not self.use_animation:
            # 静止画：次のVLへ。次VLでstartにリセットする
            self._vl_index += 1
            self._need_reset_frame = True
        else:
            # アニメ：次フレームへ。レンジを超えたら次のVLへ
            self._frame += step
            if self._frame > end:
                self._vl_index += 1
                self._need_reset_frame = True  # ★ 次VLのstartへ

        return {'RUNNING_MODAL'}


# =========================================================
# register / unregister
# =========================================================
classes = (
    COLM_OT_set_collection_override,
    COLM_OT_clear_collection_override,
    VLM_OT_prepare_output_nodes,
    VLM_OT_render_active_viewlayer,
    VLM_OT_render_all_viewlayers,
)

def register():
    for c in classes:
        bpy.utils.register_class(c)

def unregister():
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
