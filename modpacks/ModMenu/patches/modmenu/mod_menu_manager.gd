extends Node

# Same trick as net_manager.gd's "ONLINE (F9)" button: a floating, polled CanvasLayer
# button rather than hooking menu_home.tscn directly, so this stays independent of any other modpack touching that scene.

const ENTRY_INSET: Vector2 = Vector2(24, 8)
# one layer under the multiplayer mod's own entry button (128), so if both
# mods are enabled together neither sits on top of the other by accident.
const LAYER: int = 127

var _entry_layer: CanvasLayer
var _entry_button: Button
var _overlay: Control


func _ready() -> void:
	_entry_layer = CanvasLayer.new()
	_entry_layer.layer = LAYER
	add_child(_entry_layer)

	_entry_button = Button.new()
	_entry_button.text = "MODS"
	_entry_button.tooltip_text = "See which mods are loaded in this build."
	_entry_button.focus_mode = Control.FOCUS_NONE
	_entry_button.pressed.connect(toggle_menu)
	_entry_layer.add_child(_entry_button)

	var ticker: Timer = Timer.new()
	ticker.wait_time = 0.4
	ticker.timeout.connect(_place_entry_button)
	add_child(ticker)
	ticker.start()
	_place_entry_button()


func _place_entry_button() -> void:
	if not is_instance_valid(_entry_button):
		return
	_entry_button.visible = _can_open_menu()
	if not _entry_button.visible:
		return
	_entry_button.size = _entry_button.get_combined_minimum_size()
	# Bottom-right: top-right is taken by the multiplayer mod's button and the game's version text.
	var view: Vector2 = _entry_layer.get_viewport().get_visible_rect().size
	_entry_button.position = Vector2(
		view.x - _entry_button.size.x - ENTRY_INSET.x,
		view.y - _entry_button.size.y - ENTRY_INSET.y)


# Main menu only, same reasoning as net_manager.gd: opening this mid-match or
# mid-dub would sit an unrelated overlay on top of gameplay for no reason, and
# menu_master hosts every menu screen (customize, settings, credits, guides,
# ...) as one child of its SlideCapsule, so checking for menu_master alone
# would show the button on all of them instead of just the home screen.
func _can_open_menu() -> bool:
	if is_instance_valid(_overlay):
		return true  # keep the button up so it can also close the overlay
	if not get_tree().get_root().has_node("World"):
		return false
	var world: Node = M.world
	var blackout: CanvasItem = world.blackout
	if is_instance_valid(blackout) and blackout.visible:
		return false
	var capsule: Node = world.primary_capsule
	if capsule == null:
		return false
	for child: Node in capsule.get_children():
		if not child.scene_file_path.contains("menu_master"):
			continue
		var slide_capsule: Node = child.get_node_or_null("SlideCapsule")
		if slide_capsule == null:
			return false
		for slide: Node in slide_capsule.get_children():
			if slide.scene_file_path.contains("menu_home"):
				return true
		return false
	return false


func toggle_menu() -> void:
	if is_instance_valid(_overlay):
		_overlay.queue_free()
		_overlay = null
		return
	if not _can_open_menu():
		return
	_overlay = preload("res://modmenu/mod_menu_overlay.gd").new()
	_entry_layer.add_child(_overlay)
