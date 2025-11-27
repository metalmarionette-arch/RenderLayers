# render_override.py
#
# ビューレイヤーごとのレンダー設定 + カメラ / フォーマット / フレーム範囲オーバーライド
# ------------------------------------------------------------

import bpy
from bpy.props import (
    EnumProperty,
    IntProperty,
    BoolProperty,
    PointerProperty,
    FloatProperty,
)
from bpy.types import PropertyGroup, Panel
from bpy.app.handlers import persistent

# ──────────────────────────────────────────────
# ① ビューレイヤーごとのレンダー設定を保持する PropertyGroup
# ──────────────────────────────────────────────

# 既存のヘルパー/定数（例）
ENGINES = [
    ("BLENDER_EEVEE_NEXT", "Eevee Next", ""),
    ("BLENDER_WORKBENCH",  "Workbench",  ""),
    ("CYCLES",             "Cycles",     ""),
]

# 追加（ファイル上部のユーティリティ群の近くに）
def _normalize_engine_id(val: str) -> str:
    if not val:
        return "BLENDER_EEVEE_NEXT"
    # 旧識別子 → 新識別子に統一
    if val in {"BLENDER_EEVEE", "EEVEE"}:
        return "BLENDER_EEVEE_NEXT"
    # 想定外は安全側で Eevee Next に寄せる
    if val not in {"BLENDER_EEVEE_NEXT", "BLENDER_WORKBENCH", "CYCLES"}:
        return "BLENDER_EEVEE_NEXT"
    return val

def _sanitize_engine_values(scene: bpy.types.Scene) -> None:
    # 既存 .blend に保存されている旧値をその場で矯正
    for vl in scene.view_layers:
        vrs = getattr(vl, "vlm_render", None)
        if vrs is not None:
            fixed = _normalize_engine_id(getattr(vrs, "engine", ""))
            if fixed != getattr(vrs, "engine", ""):
                vrs.engine = fixed

def _update_render_settings(self, context):
    pass
def _camera_poll(self, obj):
    """カメラだけを選択肢にするポーラ―関数"""
    return obj.type == 'CAMERA'

# --- update コールバック関数 ---
def _update_render_settings(self, context):
    """プロパティ更新時にレンダー設定を適用し、ビューポートを更新する"""
    if context.scene and context.view_layer:
        if not context.scene.get("vlm_settings_synced", False):
            return
        apply_render_override(context.scene, context.view_layer)
        
        for window in context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()

# ---- VLM_RenderSettings（このクラス全体で置換）----
class VLM_RenderSettings(PropertyGroup):
    # 各VLでエンジンを上書きするか
    engine_enable: BoolProperty(
        name="Engine Override",
        description="このレイヤーでレンダーエンジンを上書きする",
        default=False,
        update=_update_render_settings
    )

    # レンダーエンジン
    engine: EnumProperty(
        name="Render Engine",
        items=ENGINES,  # ファイル先頭の既存ENGINESを使用
        default="BLENDER_EEVEE_NEXT",
        update=_update_render_settings
    )

    # ★ 各VLのサンプル数を使うか（新規）
    samples_enable: BoolProperty(
        name="Use Samples on this Layer",
        description="このレイヤーのサンプル数を使用（OFFなら先頭レイヤーのSamplesにフォールバック）",
        default=False,
        update=_update_render_settings
    )

    # サンプル数
    samples: IntProperty(
        name="Samples",
        description="Cycles/Eeveeのサンプル数（1-4096）",
        default=64, min=1, max=4096,
        update=_update_render_settings
    )

    # Cycles用デノイズ
    use_denoise: BoolProperty(
        name="Denoise",
        description="Cyclesでデノイズを有効にする",
        default=False,
        update=_update_render_settings
    )

    # カメラ上書き
    camera_enable: BoolProperty(
        name="Camera Override",
        default=False,
        update=_update_render_settings
    )
    camera: PointerProperty(
        name="Camera",
        type=bpy.types.Object,
        poll=_camera_poll,
        update=_update_render_settings
    )

    # World上書き
    world_enable: BoolProperty(
        name="World Override",
        default=False,
        update=_update_render_settings
    )

    # 出力フォーマット上書き
    format_enable: BoolProperty(
        name="Format Override",
        default=False,
        update=_update_render_settings
    )
    resolution_x: IntProperty(name="解像度 X", default=1920, min=1,  max=16384, update=_update_render_settings)
    resolution_y: IntProperty(name="解像度 Y", default=1080, min=1,  max=16384, update=_update_render_settings)
    resolution_percentage: IntProperty(name="Scale (%)", default=100, min=1, max=1000, update=_update_render_settings)
    aspect_x: FloatProperty(name="アスペクト X", default=1.0, min=0.1, update=_update_render_settings)
    aspect_y: FloatProperty(name="アスペクト Y", default=1.0, min=0.1, update=_update_render_settings)
    frame_rate: FloatProperty(name="FPS", default=24.0, min=0.01, max=60000, precision=3, update=_update_render_settings)

    # フレーム範囲上書き
    frame_enable: BoolProperty(
        name="Frame Range Override",
        default=False,
        update=_update_render_settings
    )
    frame_start: IntProperty(name="Start", default=1,   min=0, update=_update_render_settings)
    frame_end:   IntProperty(name="End",   default=250, min=0, update=_update_render_settings)
    frame_step:  IntProperty(name="Step",  default=1,   min=1, update=_update_render_settings)
# ---- /VLM_RenderSettings ----


# ──────────────────────────────────────────────
# ② 既存のシーン設定を取得してアドオンパラメーターに反映する関数
# ──────────────────────────────────────────────
def sync_scene_settings_to_addon(scene):
    """
    シーンの既存設定を、アドオンの各ビューレイヤープロパティに反映する
    """
    print("VLM: シーン設定を全ビューレイヤーに同期中...")
    
    r = scene.render
    
    for vl in scene.view_layers:
        rs = vl.vlm_render
        
        if r.engine in {'BLENDER_EEVEE', 'BLENDER_EEVEE_NEXT'}:
            rs.engine = 'BLENDER_EEVEE_NEXT'
        elif r.engine == 'CYCLES':
            rs.engine = 'CYCLES'
        
        if rs.engine == 'CYCLES' and hasattr(scene, 'cycles'):
            rs.samples     = scene.cycles.samples
            rs.use_denoise = scene.cycles.use_denoising
        elif rs.engine == 'BLENDER_EEVEE_NEXT' and hasattr(scene, 'eevee'):
            rs.samples = scene.eevee.taa_render_samples
        
        if scene.camera:
            rs.camera = scene.camera
        
        if scene.world:
            vl.vlm_world = scene.world
        
        rs.resolution_x = r.resolution_x
        rs.resolution_y = r.resolution_y
        rs.resolution_percentage = r.resolution_percentage
        rs.aspect_x = r.pixel_aspect_x
        rs.aspect_y = r.pixel_aspect_y
        # `fps` と `fps_base` から実際のフレームレートを計算して反映
        rs.frame_rate = r.fps / r.fps_base
        
        rs.frame_start = scene.frame_start
        rs.frame_end = scene.frame_end
        rs.frame_step = scene.frame_step

    scene["vlm_settings_synced"] = True
    print("VLM: シーン設定の全ビューレイヤーへの同期が完了しました")

    # ★ 先頭ビューレイヤー（基準レイヤー）の固定
    if scene.view_layers:
        top_vl = scene.view_layers[0]
        top_rs = top_vl.vlm_render
        # 各オーバーライドの基準ON
        top_rs.engine_enable = True
        top_rs.camera_enable = True
        top_rs.format_enable = True
        top_rs.frame_enable  = True
        top_rs.world_enable  = True  # ← World も基準ON

        # Worldの初期化（未設定なら Scene.world を基準に）
        if not getattr(top_vl, "vlm_world", None) and scene.world:
            top_vl.vlm_world = scene.world

    scene["vlm_settings_synced"] = True

# ──────────────────────────────────────────────
# ③ ファイル読み込み後ハンドラ (変更なし)
# ──────────────────────────────────────────────
@persistent
def load_post_handler(_dummy):
    scene = bpy.context.scene
    if scene is None:
        return

    if scene.get("vlm_settings_synced", False):
        return

    sync_scene_settings_to_addon(scene)

    if scene.view_layers:
        top_vl = scene.view_layers[0]
        apply_render_override(scene, top_vl)
        
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()

# ──────────────────────────────────────────────
# ④ プロパティエディタ > レンダータブ内に簡易表示（任意）(変更なし)
# ──────────────────────────────────────────────
class VLM_PT_RenderOverride(Panel):
    bl_label = "ViewLayer Render Override"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "render"

    @classmethod
    def poll(cls, context):
        return context.view_layer is not None

    def draw(self, context):
        layout = self.layout
        rs = context.view_layer.vlm_render
        layout.use_property_split = True
        layout.prop(rs, "engine")
        if rs.engine == 'CYCLES':
            col = layout.column()
            col.prop(rs, "samples")
            col.prop(rs, "use_denoise")

# ──────────────────────────────────────────────
# ⑤ Scene へ反映するヘルパー
# ──────────────────────────────────────────────
def apply_render_override(scene: bpy.types.Scene,
                          view_layer: bpy.types.ViewLayer):
    """サンプル数は【強制】＞【各VLサンプルON】＞【先頭VLのSamples】の優先順位で適用。
       レンダーエンジンは旧識別子（BLENDER_EEVEE）を互換変換してから代入。"""
    r = scene.render

    # 既存 .blend の旧値を先に正規化
    _sanitize_engine_values(scene)

    # 先頭VL（フォールバック元）
    top_vl = scene.view_layers[0] if scene.view_layers else view_layer
    rs     = getattr(view_layer, "vlm_render", None)
    top_rs = getattr(top_vl,     "vlm_render", None)
    if rs is None or top_rs is None:
        return

    # 1) レンダーエンジン（engine_enable が True ならこのVL、Falseなら先頭VL）
    eng_src = rs if (view_layer == top_vl or getattr(rs, "engine_enable", False)) else top_rs
    r.engine = _normalize_engine_id(getattr(eng_src, "engine", "BLENDER_EEVEE_NEXT"))

    # 2) サンプルの解決（強制 ＞ 各VLトグルON ＞ 先頭VL）
    def _resolve_samples():
        if getattr(scene, "vlm_force_samples_enable", False):
            return None  # 後段で強制値を適用
        if bool(getattr(rs, "samples_enable", False)):
            return max(1, int(getattr(rs, "samples", 1)))
        return max(1, int(getattr(top_rs, "samples", 1)))

    samples_val = _resolve_samples()

    # 3) エンジン別にサンプル適用
    if r.engine == 'CYCLES':
        if samples_val is not None:
            scene.cycles.samples = samples_val
        # デノイズはエンジン選択元に追随（UIから削除していても内部値は尊重）
        scene.cycles.use_denoising = bool(getattr(eng_src, "use_denoise", False))

    elif r.engine in {'BLENDER_EEVEE_NEXT'}:
        if samples_val is not None:
            scene.eevee.taa_render_samples = samples_val

    elif r.engine == 'BLENDER_WORKBENCH':
        # Workbench はパストレ数の概念なし（何もしない）
        pass

    # 3.5) 強制サンプル（最優先で最終上書き）
    if getattr(scene, "vlm_force_samples_enable", False):
        if r.engine == 'CYCLES':
            scene.cycles.samples = max(1, int(getattr(scene, "vlm_force_samples_cycles", 16)))
        elif r.engine == 'BLENDER_EEVEE_NEXT':
            scene.eevee.taa_render_samples = max(1, int(getattr(scene, "vlm_force_samples_eevee", 16)))
        else:
            # Workbench 等では無視
            pass

    # 4) カメラ（従来どおり）
    if view_layer == top_vl:
        if rs.camera:
            scene.camera = rs.camera
    else:
        cam = rs.camera if (getattr(rs, "camera_enable", False) and rs.camera) else top_rs.camera
        if cam:
            scene.camera = cam

    # 5) フォーマット（従来どおり）
    fmt = rs if (view_layer == top_vl or getattr(rs, "format_enable", False)) else top_rs
    scale  = max(1, min(int(fmt.resolution_percentage), 1000))
    factor = scale / 100.0
    max_dim = 16384
    if scale <= 100:
        r.resolution_x          = fmt.resolution_x
        r.resolution_y          = fmt.resolution_y
        r.resolution_percentage = scale
    else:
        r.resolution_x          = min(int(round(fmt.resolution_x * factor)), max_dim)
        r.resolution_y          = min(int(round(fmt.resolution_y * factor)), max_dim)
        r.resolution_percentage = 100
    r.pixel_aspect_x = fmt.aspect_x
    r.pixel_aspect_y = fmt.aspect_y

    # 6) フレームレート（従来どおり）
    fr = round(fmt.frame_rate, 3)
    if round(fr, 2) == 29.97:
        r.fps = 30000; r.fps_base = 1001.0
    elif fr == 23.976:
        r.fps = 24000; r.fps_base = 1001.0
    else:
        r.fps = int(round(fr)); r.fps_base = 1.0

    # 7) フレーム範囲（従来どおり）
    frm = rs if (view_layer == top_vl or getattr(rs, "frame_enable", False)) else top_rs
    start = max(0, int(frm.frame_start))
    end   = max(start, int(frm.frame_end))
    step  = max(1, int(frm.frame_step))
    scene.frame_start = start
    scene.frame_end   = end
    scene.frame_step  = step

    # 8) World（従来どおり）
    base_world = getattr(top_vl, "vlm_world", None) or scene.world
    if view_layer == top_vl:
        scene.world = base_world
    else:
        use_vl_world = getattr(rs, "world_enable", False) and getattr(view_layer, "vlm_world", None)
        scene.world = view_layer.vlm_world if use_vl_world else base_world

# ──────────────────────────────────────────────
# ⑥ 手動同期オペレーター (変更なし)
# ──────────────────────────────────────────────
class VLM_OT_sync_scene_settings(bpy.types.Operator):
    bl_idname = "vlm.sync_scene_settings"
    bl_label = "シーン設定を再同期"
    bl_description = "現在のシーンの出力設定をアドオンに強制的に再同期します"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if "vlm_settings_synced" in context.scene:
            del context.scene["vlm_settings_synced"]
        load_post_handler(None)
        self.report({'INFO'}, "シーン設定をアドオンパラメーターに再同期しました")
        return {'FINISHED'}

# ──────────────────────────────────────────────
# ⑦ register / unregister (変更なし)
# ──────────────────────────────────────────────
classes = (
    VLM_RenderSettings,
    VLM_PT_RenderOverride,
    VLM_OT_sync_scene_settings,
)

def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.ViewLayer.vlm_render = PointerProperty(type=VLM_RenderSettings)
    
    def _update_world_settings(self, context):
        if context.scene and context.view_layer:
            if not context.scene.get("vlm_settings_synced", False):
                return
            apply_render_override(context.scene, context.view_layer)
            for window in context.window_manager.windows:
                for area in window.screen.areas:
                    if area.type == 'VIEW_3D':
                        area.tag_redraw()
    
    bpy.types.ViewLayer.vlm_world = PointerProperty(
        name="World",
        type=bpy.types.World,
        update=_update_world_settings
    )
    
    if load_post_handler not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(load_post_handler)

    if not hasattr(bpy.types.ViewLayer, "vlm_world"):
        bpy.types.ViewLayer.vlm_world = bpy.props.PointerProperty(
            name="World",
            type=bpy.types.World,
            description="このビューレイヤーで使用する World（環境）"
        )


def unregister():

    if load_post_handler in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(load_post_handler)
    
    if hasattr(bpy.types.ViewLayer, 'vlm_render'):
        del bpy.types.ViewLayer.vlm_render
    if hasattr(bpy.types.ViewLayer, 'vlm_world'):
        del bpy.types.ViewLayer.vlm_world
        
    for c in reversed(classes):
        try:
            bpy.utils.unregister_class(c)
        except RuntimeError:
            # アドオンのリロード時に発生することがあるが、無視してよい
            pass