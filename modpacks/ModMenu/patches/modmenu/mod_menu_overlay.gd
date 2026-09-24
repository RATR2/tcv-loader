extends Control

# Reads res://mods_manifest.json (install.py's apply_combined_mod() bakes it in from
# combine.py's build_manifest.json): the only tie to installed mods; loadorder.json/modpacks/ don't exist once exported.

const MANIFEST_PATH: String = "res://mods_manifest.json"

# Matches combine.py's build time limits for local images; remote images skip
# that check entirely (combine.py never downloads them), so this is the only
# place stopping a mod from pointing at a huge file and stalling or crashing
# the running game.
const MAX_IMAGE_BYTES: int = 512 * 1024
const MAX_IMAGE_DIMENSION: int = 512


var _backdrop: ColorRect
var _margin: MarginContainer


func _ready() -> void:
	# Percentage anchors don't resolve against the viewport for a Control parented straight
	# under a bare CanvasLayer (unlike lobby.gd's, in an already-sized container); skip
	# anchors here and drive size/position as absolute pixels, resynced on resize.
	mouse_filter = Control.MOUSE_FILTER_STOP
	_build_ui()
	_resync_size()
	get_viewport().size_changed.connect(_resync_size)


func _resync_size() -> void:
	var view_size: Vector2 = get_viewport().get_visible_rect().size
	for node: Control in [self, _backdrop, _margin]:
		node.anchor_left = 0.0
		node.anchor_top = 0.0
		node.anchor_right = 0.0
		node.anchor_bottom = 0.0
		node.position = Vector2.ZERO
		node.size = view_size


func _build_ui() -> void:
	_backdrop = ColorRect.new()
	_backdrop.color = Color(0.05, 0.05, 0.09, 0.94)
	add_child(_backdrop)

	_margin = MarginContainer.new()
	var margin: MarginContainer = _margin
	for side: String in ["left", "right", "top", "bottom"]:
		margin.add_theme_constant_override("margin_" + side, 64)
	add_child(margin)

	var column: VBoxContainer = VBoxContainer.new()
	column.add_theme_constant_override("separation", 10)
	margin.add_child(column)

	var title: Label = Label.new()
	title.text = "Mods loaded in this build"
	title.add_theme_font_size_override("font_size", 28)
	column.add_child(title)

	var entries: Array = _load_manifest()
	if entries.is_empty():
		var none_label: Label = Label.new()
		none_label.text = "No mod manifest found. This build may predate the mod menu."
		none_label.modulate.a = 0.75
		column.add_child(none_label)

	var scroll: ScrollContainer = ScrollContainer.new()
	scroll.size_flags_vertical = Control.SIZE_EXPAND_FILL
	column.add_child(scroll)

	var list: VBoxContainer = VBoxContainer.new()
	list.add_theme_constant_override("separation", 18)
	list.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	scroll.add_child(list)

	for entry: Dictionary in entries:
		list.add_child(_build_row(entry))

	var close_button: Button = Button.new()
	close_button.text = "Close"
	close_button.pressed.connect(queue_free)
	close_button.custom_minimum_size = Vector2(140, 36)
	close_button.size_flags_horizontal = Control.SIZE_SHRINK_CENTER
	close_button.add_theme_color_override("font_color", Color(1, 1, 1, 1))
	close_button.add_theme_color_override("font_hover_color", Color(1, 1, 1, 1))
	# The base game's Theme gives a plain Button no visible panel against a dark background; explicit styleboxes here instead of relying on the project default.
	var normal_style: StyleBoxFlat = StyleBoxFlat.new()
	normal_style.bg_color = Color(0.24, 0.26, 0.32, 1.0)
	normal_style.border_color = Color(0.55, 0.6, 0.68, 1.0)
	normal_style.set_border_width_all(2)
	normal_style.set_corner_radius_all(6)
	close_button.add_theme_stylebox_override("normal", normal_style)
	var hover_style: StyleBoxFlat = normal_style.duplicate()
	hover_style.bg_color = Color(0.34, 0.37, 0.45, 1.0)
	close_button.add_theme_stylebox_override("hover", hover_style)
	var pressed_style: StyleBoxFlat = normal_style.duplicate()
	pressed_style.bg_color = Color(0.18, 0.2, 0.25, 1.0)
	close_button.add_theme_stylebox_override("pressed", pressed_style)
	column.add_child(close_button)


# Deterministic per-id color so the placeholder icon isn't a wall of
# identical squares once more than a couple mods are loaded. Mirrors the
# desktop GUI's colorForId() in app.js, though not pixel-identical since
# that's CSS HSL and this is Godot's HSV.
func _color_for_id(id: String) -> Color:
	var hash_value: int = 0
	for byte: int in id.to_utf8_buffer():
		hash_value = (hash_value * 31 + byte) & 0xFFFFFFFF
	var hue: float = float(hash_value % 360) / 360.0
	return Color.from_hsv(hue, 0.45, 0.62)


func _load_manifest() -> Array:
	if not FileAccess.file_exists(MANIFEST_PATH):
		return []
	var text: String = FileAccess.get_file_as_string(MANIFEST_PATH)
	var data: Variant = JSON.parse_string(text)
	if typeof(data) != TYPE_DICTIONARY:
		return []
	return data.get("modpacks", [])


func _build_row(entry: Dictionary) -> Control:
	var card: PanelContainer = PanelContainer.new()
	var card_style: StyleBoxFlat = StyleBoxFlat.new()
	card_style.bg_color = Color(0.14, 0.15, 0.19, 1.0)
	card_style.border_color = Color(0.3, 0.32, 0.38, 1.0)
	card_style.set_border_width_all(1)
	card_style.set_corner_radius_all(8)
	card_style.set_content_margin_all(14)
	card.add_theme_stylebox_override("panel", card_style)

	var row: HBoxContainer = HBoxContainer.new()
	row.add_theme_constant_override("separation", 16)
	card.add_child(row)

	var icon_holder: Control = Control.new()
	icon_holder.custom_minimum_size = Vector2(64, 64)
	# HBoxContainer stretches children to the row's full height by default,
	# which would otherwise turn this into a tall rectangle for any entry
	# whose description wraps to more than one line.
	icon_holder.size_flags_vertical = Control.SIZE_SHRINK_CENTER
	row.add_child(icon_holder)

	var name: String = String(entry.get("name", "?"))
	var placeholder: ColorRect = ColorRect.new()
	placeholder.color = _color_for_id(String(entry.get("id", name)))
	placeholder.set_anchors_preset(Control.PRESET_FULL_RECT)
	icon_holder.add_child(placeholder)

	var letter: Label = Label.new()
	letter.text = name.strip_edges().substr(0, 1).to_upper()
	letter.add_theme_font_size_override("font_size", 30)
	letter.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	letter.vertical_alignment = VERTICAL_ALIGNMENT_CENTER
	letter.set_anchors_preset(Control.PRESET_FULL_RECT)
	icon_holder.add_child(letter)

	# Left transparent until (if) a real image loads, so the colored letter
	# placeholder above shows through for mods with no image, a broken image
	# URL, or one rejected by the size/dimension guard in _load_image().
	var icon: TextureRect = TextureRect.new()
	icon.set_anchors_preset(Control.PRESET_FULL_RECT)
	icon.expand_mode = TextureRect.EXPAND_FIT_HEIGHT_PROPORTIONAL
	icon.stretch_mode = TextureRect.STRETCH_KEEP_ASPECT_CENTERED
	icon_holder.add_child(icon)
	_load_image(String(entry.get("image", "")), icon)

	var text_col: VBoxContainer = VBoxContainer.new()
	text_col.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	row.add_child(text_col)

	var name_label: Label = Label.new()
	name_label.text = "%s  v%s" % [entry.get("name", "?"), entry.get("version", "?")]
	name_label.add_theme_font_size_override("font_size", 20)
	text_col.add_child(name_label)

	var meta_label: Label = Label.new()
	meta_label.text = "by %s" % entry.get("author", "unknown")
	meta_label.modulate.a = 0.75
	text_col.add_child(meta_label)

	var description: String = String(entry.get("description", ""))
	if not description.is_empty():
		var desc_label: Label = Label.new()
		desc_label.text = description
		desc_label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
		text_col.add_child(desc_label)

	return card


# meta.image is a res:// path combine.py already copied locally, or an http(s) URL pasted straight
# off e.g. GitHub, fetched here at runtime rather than at build time, since the build pipeline shouldn't require network access.
func _load_image(path: String, into: TextureRect) -> void:
	if path.is_empty():
		return
	if path.begins_with("http://") or path.begins_with("https://"):
		var request: HTTPRequest = HTTPRequest.new()
		add_child(request)
		request.request_completed.connect(func(result: int, code: int,
				_headers: PackedStringArray, body: PackedByteArray) -> void:
			request.queue_free()
			if result != HTTPRequest.RESULT_SUCCESS or code != 200:
				return
			if body.size() > MAX_IMAGE_BYTES:
				return
			var img: Image = Image.new()
			var err: Error = img.load_png_from_buffer(body)
			if err != OK:
				err = img.load_jpg_from_buffer(body)
			if err != OK:
				err = img.load_webp_from_buffer(body)
			if err != OK or not is_instance_valid(into):
				return
			_clamp_image_dimensions(img)
			into.texture = ImageTexture.create_from_image(img)
		)
		request.request(path)
		return
	if ResourceLoader.exists(path):
		var res: Resource = load(path)
		if res is Texture2D:
			into.texture = res


# Downscales in place, keeping aspect ratio, if either side is over the cap.
func _clamp_image_dimensions(img: Image) -> void:
	var width: int = img.get_width()
	var height: int = img.get_height()
	var largest: int = max(width, height)
	if largest <= MAX_IMAGE_DIMENSION:
		return
	var scale: float = float(MAX_IMAGE_DIMENSION) / float(largest)
	img.resize(max(1, roundi(width * scale)), max(1, roundi(height * scale)), Image.INTERPOLATE_LANCZOS)
