import bpy
import json

# --------------------------------------------------
# ViewLayer に保存するライト状態辞書（JSON 文字列）
# --------------------------------------------------
def _get_light_state_dict(vl):
    raw = getattr(vl, "vlm_light_state_dict", "")
    return json.loads(raw) if raw else {}

def _set_light_state_dict(vl, d):
    vl.vlm_light_state_dict = json.dumps(d)

# --------------------------------------------------
# 現在のライト表示状態を記憶
# --------------------------------------------------
class VLM_OT_memorize_lights(bpy.types.Operator):
    bl_idname = "vlm.memorize_lights"
    bl_label  = "ライト状態を記憶"

    def execute(self, context):
        vl = context.view_layer
        d = {}
        for ob in bpy.data.objects:
            if ob.type == 'LIGHT':
                d[ob.name] = {
                    "hide_viewport": ob.hide_viewport,
                    "hide_render": ob.hide_render,
                }
        _set_light_state_dict(vl, d)
        self.report({'INFO'}, "ライト状態を記憶しました")
        return {'FINISHED'}

# --------------------------------------------------
# 記憶したライト表示状態を即時反映
# --------------------------------------------------
class VLM_OT_apply_memorized_lights(bpy.types.Operator):
    bl_idname = "vlm.apply_memorized_lights"
    bl_label  = "記憶状態を反映"

    def execute(self, context):
        apply_lights_for_viewlayer(context.view_layer)
        self.report({'INFO'}, "記憶した状態を反映しました")
        return {'FINISHED'}

# --------------------------------------------------
# 指定 ViewLayer 用のライト状態を反映するユーティリティ
#   * レンダリングオペレーターからも呼び出し
# --------------------------------------------------
# light_camera.py

def apply_lights_for_viewlayer(vl, *, do_view_update=True):
    state = _get_light_state_dict(vl)

    # このビューレイヤーに属するオブジェクト名セットを作成
    vl_obj_names = {o.name for o in vl.objects}

    for ob in bpy.data.objects:
        if ob.type != 'LIGHT':
            continue

        # ▼ ビューレイヤーに存在しないライトはスキップ（コレクションがOFFなど）
        if ob.name not in vl_obj_names:
            continue

        rec = state.get(ob.name)
        if rec:
            hide_rnd = bool(rec.get("hide_render",   False))
            hide_vp  = bool(rec.get("hide_viewport", False))
            if hide_rnd:
                hide_vp = True
        else:
            hide_rnd = False
            hide_vp  = False

        ob.hide_render = hide_rnd
        try:
            ob.hide_set(hide_vp)   # ここは環境により RuntimeError が出るので保護
        except RuntimeError:
            pass

    # ★ F12直前はビュー更新を抑止して安全性を優先
    if do_view_update:
        try:
            bpy.context.view_layer.update()
        except Exception:
            pass

# --------------------------------------------------
# register / unregister
# --------------------------------------------------
classes = (
    VLM_OT_memorize_lights,
    VLM_OT_apply_memorized_lights,
)

def register():
    for c in classes:
        bpy.utils.register_class(c)
    # ViewLayer に JSON 文字列プロパティを用意
    bpy.types.ViewLayer.vlm_light_state_dict = bpy.props.StringProperty(
        name="ライト状態辞書",
        default="",
        description="記憶したライトの表示/非表示状態 (JSON)",
    )

def unregister():
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
    del bpy.types.ViewLayer.vlm_light_state_dict
