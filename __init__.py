# __init__.py  — clean & safe

bl_info = {
    "name":        "View Layer Manager (Light & Collection)",
    "author":      "Your Name",
    "version":     (1, 6, 2),
    "blender":     (4, 0, 0),
    "location":    "3D View > N Panel > RenderLayers",
    "description": "Manage view layers, collections, light/camera, render overrides, and safe rendering options.",
    "category":    "Render",
}

import bpy
from bpy.props import (
    StringProperty, BoolProperty, IntProperty, FloatProperty, EnumProperty
)

# ▼ ここで一度だけモジュールを import（register/unregister 内で再importしない）
from . import (
    render_override,
    main_panel,
    collection_management,
    material_override,
    light_camera,
    viewlayer_operations,
)

# ----------------------------------------------------------------
# register / unregister
# ----------------------------------------------------------------
def register():
    # --- 既存の簡易プロパティ ---
    bpy.types.Collection.vlm_temp_mat = StringProperty(
        name="Temp Material",
        description="コレクションに適用するマテリアル（未確定）",
        default=""
    )
    bpy.types.ViewLayer.vlm_render_this_layer = BoolProperty(
        name="Render this layer",
        description="このビューレイヤーを『全Layersレンダリング』に含める",
        default=True
    )

    # --- 折りたたみUIの状態（Scene に保存） ---
    bpy.types.Scene.vlm_ui_show_collections      = BoolProperty(default=True)
    bpy.types.Scene.vlm_ui_show_mat_backup       = BoolProperty(default=True)
    bpy.types.Scene.vlm_ui_show_render_engine    = BoolProperty(default=True)
    bpy.types.Scene.vlm_ui_show_camera           = BoolProperty(default=False)
    bpy.types.Scene.vlm_ui_show_world            = BoolProperty(default=False)
    bpy.types.Scene.vlm_ui_show_lights           = BoolProperty(default=False)
    bpy.types.Scene.vlm_ui_show_format           = BoolProperty(default=False)
    bpy.types.Scene.vlm_ui_show_frame_range      = BoolProperty(default=False)
    bpy.types.Scene.vlm_ui_show_output_nodes     = BoolProperty(default=False)
    bpy.types.Scene.vlm_ui_show_render_output    = BoolProperty(default=True)
    bpy.types.Scene.vlm_ui_show_sample_override  = BoolProperty(default=False)
    bpy.types.Scene.vlm_ui_show_cycles_light_paths = BoolProperty(default=False)

    bpy.types.Scene.vlm_skip_existing_frames = BoolProperty(
        name="Skip Existing Frames",
        description="既に書き出されたフレームがあればスキップして次のフレームからレンダーを続行する",
        default=False,
    )

    # --- サンプル強制上書き（Scene） ---
    def _update_force_samples(self, context):
        try:
            from .render_override import apply_render_override
            apply_render_override(context.scene, context.view_layer)
        except Exception:
            pass

    bpy.types.Scene.vlm_force_samples_enable = BoolProperty(
        name="Force Render Samples", default=False, update=_update_force_samples
    )
    bpy.types.Scene.vlm_force_samples_cycles = IntProperty(
        name="Cycles Samples (Force)", default=16, min=1, max=4096, update=_update_force_samples
    )
    bpy.types.Scene.vlm_force_samples_eevee = IntProperty(
        name="Eevee Samples (Force)", default=16, min=1, max=4096, update=_update_force_samples
    )


    # --- モジュール登録（トップの import を利用） ---
    render_override.register()
    main_panel.register()
    collection_management.register()
    material_override.register()
    light_camera.register()
    viewlayer_operations.register()

def unregister():
    # --- 実行中の外部レンダをまず停止（プロパティ削除より前） ---
    try:
        from .collection_management import _kill_current_external_render
        _kill_current_external_render()
    except Exception:
        pass

    # --- モジュールの unregister（逆順） ---
    try: viewlayer_operations.unregister()
    except Exception: pass
    try: light_camera.unregister()
    except Exception: pass
    try: material_override.unregister()
    except Exception: pass
    try: collection_management.unregister()
    except Exception: pass
    try: main_panel.unregister()
    except Exception: pass
    try: render_override.unregister()
    except Exception: pass

    # --- 追加プロパティの削除（存在チェックつき） ---
    def _del(tp, name):
        if hasattr(tp, name):
            try: delattr(tp, name)
            except Exception: pass

    # Scene
    for nm in (
        "vlm_ui_show_collections","vlm_ui_show_mat_backup","vlm_ui_show_render_engine",
        "vlm_ui_show_camera","vlm_ui_show_world","vlm_ui_show_lights","vlm_ui_show_format",
        "vlm_ui_show_frame_range","vlm_ui_show_output_nodes","vlm_ui_show_render_output",
        "vlm_ui_show_sample_override",
        "vlm_ui_show_cycles_light_paths",
        "vlm_skip_existing_frames",
        "vlm_force_samples_enable","vlm_force_samples_cycles","vlm_force_samples_eevee",
        "vlm_gpu_safe_mode",
        "vlm_vram_watch_enable","vlm_vram_threshold_pct","vlm_vram_warmup_frames",
        "vlm_vram_safety_margin","vlm_vram_action",
        "vlm_isolated_enable","vlm_isolated_chunk","vlm_isolated_engine","vlm_isolated_persistent_off",
    ):
        _del(bpy.types.Scene, nm)

    # WindowManager
    for nm in ("vlm_isolated_running","vlm_isolated_status"):
        _del(bpy.types.WindowManager, nm)

    # Collection / ViewLayer
    _del(bpy.types.Collection, "vlm_temp_mat")
    _del(bpy.types.ViewLayer,  "vlm_render_this_layer")


if __name__ == "__main__":
    register()
