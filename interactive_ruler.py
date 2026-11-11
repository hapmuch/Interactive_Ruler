# -*- coding: utf-8 -*-
"""
QGIS Interactive Ruler Plugin
Features:
1. A single toolbar button opens a small popup window
2. Straight mode: drag to measure a straight line
3. Path mode: click several points and measure the total path length
4. Layer-name box: type or paste a name before measuring; the
   measurements are grouped under that name and every finished line
   or path becomes its own layer named after its result
   (SITE C 1.82 km)
5. Arrow-line style from ruler.qlr with length labels
6. Auto-switch label m <-> km
7. Live distance balloon next to the cursor while dragging
8. Label font size and line width sliders: with a Ruler layer or
   group selected in the Layers panel they restyle those existing
   lines, otherwise they set the default for new measurements
9. Dark / light / red theme asked in a small window after each
   finished line or path, with the last choice pre-selected next time;
   existing lines are never recolored - redraw to change colours
10. Refuses to measure and warns when the project has no CRS, instead
    of showing a meaningless length

Compatible: QGIS 3.x
"""

import math

from qgis.PyQt.QtCore import Qt, QDate, QSizeF, QEvent
from qgis.PyQt.QtGui import QColor, QFont, QIcon
from qgis.PyQt.QtWidgets import (
    QAction,
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QSlider,
    QVBoxLayout,
)
from qgis.gui import QgsRubberBand

from qgis.core import (
    QgsProject,
    QgsLayerTreeGroup,
    QgsVectorLayer,
    QgsField,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsPalLayerSettings,
    QgsVectorLayerSimpleLabeling,
    QgsTextFormat,
    QgsTextBackgroundSettings,
    QgsTextShadowSettings,
    QgsUnitTypes,
    QgsDistanceArea,
    QgsCoordinateTransform,
    QgsCoordinateReferenceSystem,
    QgsWkbTypes,
    QgsMarkerLineSymbolLayer,
    QgsMarkerSymbol,
    QgsLineSymbol,
    QgsFilledMarkerSymbolLayer,
    QgsSettings,
    Qgis
)

from qgis.gui import QgsMapTool

from PyQt5.QtCore import QVariant


LAYER_NAME_SETTINGS_KEY = "interactive_ruler/layer_name"
THEME_SETTINGS_KEY = "interactive_ruler/theme"
FONT_SIZE_SETTINGS_KEY = "interactive_ruler/label_font_size"
LINE_WIDTH_SETTINGS_KEY = "interactive_ruler/line_width"

# Name used when the layer-name box is empty.
DEFAULT_LAYER_NAME = "Ruler"

# Custom property that marks layers managed by this plugin.
RULER_LAYER_PROPERTY = "interactive_ruler_layer"

# Per-layer style stored as custom properties (saved with the project).
LAYER_THEME_KEY = "interactive_ruler/theme"
LAYER_FONT_KEY = "interactive_ruler/label_font_size"
LAYER_WIDTH_KEY = "interactive_ruler/line_width"

DEFAULT_THEME = "dark"
DEFAULT_FONT_SIZE = 12       # points
DEFAULT_LINE_WIDTH = 2.0     # millimetres, arrow marker size

MIN_FONT_SIZE = 6
MAX_FONT_SIZE = 24
MIN_LINE_WIDTH = 0.5
MAX_LINE_WIDTH = 6.0

# Offset of the live distance balloon from the mouse cursor, in pixels.
TOOLTIP_OFFSET_X = 14
TOOLTIP_OFFSET_Y = 18

# Icon shown on the toolbar button and in the plugin menu.
PLUGIN_ICON = "icon.svg"

# The live rubber band is always red, independent of the chosen line
# theme, so the preview stays visible on light and dark basemaps alike.
RUBBER_BAND_COLOR = QColor("red")

# Line colours per theme. "light" draws white lines and arrows so they
# stand out on dark basemaps such as Google Satellite. "red" uses the
# red from ColorBrewer's Set1 palette. The yellow label background with
# black text stays readable on every basemap, so it is shared by all
# themes.
THEMES = {
    "dark": {
        "line_color": QColor(35, 35, 35),
    },
    "light": {
        "line_color": QColor(255, 255, 255),
    },
    "red": {
        "line_color": QColor("#e31a1c"),
    },
}

LABEL_TEXT_COLOR = QColor("black")
LABEL_BACKGROUND_COLOR = QColor(255, 255, 13)


# ---------------------------------------------------------
# SETTINGS / STYLE HELPERS
# ---------------------------------------------------------
def current_settings():
    """Return (theme, font_size, line_width) saved in QSettings."""

    settings = QgsSettings()

    theme = settings.value(THEME_SETTINGS_KEY, DEFAULT_THEME, type=str)
    if theme not in THEMES:
        theme = DEFAULT_THEME

    font_size = settings.value(
        FONT_SIZE_SETTINGS_KEY, DEFAULT_FONT_SIZE, type=int
    )
    font_size = int(min(MAX_FONT_SIZE, max(MIN_FONT_SIZE, font_size)))

    line_width = settings.value(
        LINE_WIDTH_SETTINGS_KEY, DEFAULT_LINE_WIDTH, type=float
    )
    line_width = float(
        min(MAX_LINE_WIDTH, max(MIN_LINE_WIDTH, line_width))
    )

    return theme, font_size, line_width


def save_settings(theme, font_size, line_width):
    settings = QgsSettings()

    settings.setValue(THEME_SETTINGS_KEY, theme)
    settings.setValue(FONT_SIZE_SETTINGS_KEY, int(font_size))
    settings.setValue(LINE_WIDTH_SETTINGS_KEY, float(line_width))


def clamp_font_size(font_size):
    return int(min(MAX_FONT_SIZE, max(MIN_FONT_SIZE, int(font_size))))


def clamp_line_width(line_width):
    return float(
        min(MAX_LINE_WIDTH, max(MIN_LINE_WIDTH, float(line_width)))
    )


def clean_layer_name(name):
    """A usable layer name from *name* (blank or None -> 'Ruler')."""

    name = str(name or "").strip()
    return name or DEFAULT_LAYER_NAME


def unique_layer_name(base_name):
    """*base_name* with ' 2', ' 3', ... added until it is unused.

    The numbering follows the QGIS convention for pasted layers, so
    the Layers panel never ends up with two same-named layers.
    """

    base_name = clean_layer_name(base_name)

    taken = {
        layer.name()
        for layer in QgsProject.instance().mapLayers().values()
    }

    if base_name not in taken:
        return base_name

    number = 2
    while "%s %d" % (base_name, number) in taken:
        number += 1

    return "%s %d" % (base_name, number)


def layer_style(layer):
    """(theme, font_size, line_width) stored on a Ruler layer.

    Layers without stored values (e.g. from older plugin versions) fall
    back to the global default style.
    """

    theme = str(layer.customProperty(LAYER_THEME_KEY, "") or "")
    if theme not in THEMES:
        theme = DEFAULT_THEME

    try:
        font_size = clamp_font_size(
            layer.customProperty(LAYER_FONT_KEY, DEFAULT_FONT_SIZE)
        )
    except (TypeError, ValueError):
        font_size = DEFAULT_FONT_SIZE

    try:
        line_width = clamp_line_width(
            layer.customProperty(LAYER_WIDTH_KEY, DEFAULT_LINE_WIDTH)
        )
    except (TypeError, ValueError):
        line_width = DEFAULT_LINE_WIDTH

    return theme, font_size, line_width


def set_layer_style(layer, theme, font_size, line_width):
    """Store the style on *layer* (persisted in the project) and apply it."""

    if theme not in THEMES:
        theme = DEFAULT_THEME

    font_size = clamp_font_size(font_size)
    line_width = clamp_line_width(line_width)

    layer.setCustomProperty(LAYER_THEME_KEY, theme)
    layer.setCustomProperty(LAYER_FONT_KEY, font_size)
    layer.setCustomProperty(LAYER_WIDTH_KEY, line_width)

    apply_ruler_style(layer, theme, font_size, line_width)
    layer.triggerRepaint()


def is_ruler_layer(layer):
    """True for layers created by this plugin (any Ruler layer)."""

    if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
        return False

    if layer.customProperty(RULER_LAYER_PROPERTY, False):
        return True

    # Layers made by older plugin versions carry no marker property.
    # Recognise them by provider, geometry and the Length field.
    provider = layer.dataProvider()
    if provider is None or provider.name() != "memory":
        return False

    if QgsWkbTypes.geometryType(layer.wkbType()) != \
            QgsWkbTypes.LineGeometry:
        return False

    return layer.fields().lookupField("Length") != -1


def ruler_layers_in_group(group):
    """Ruler layers inside *group* (nested groups included).

    Lets the dialog restyle one site group or a whole day group
    without touching anything outside it.
    """

    layers = []

    for node in group.findLayers():
        layer = node.layer()
        if layer is not None and is_ruler_layer(layer):
            layers.append(layer)

    return layers


def apply_ruler_style(layer, theme, font_size, line_width):
    """Apply the arrow-line and length-label style to a Ruler layer.

    The style is created in code so the plugin works on any machine
    without needing the .qlr file next to the plugin.
    """

    theme_def = THEMES.get(theme, THEMES[DEFAULT_THEME])
    line_color = theme_def["line_color"]

    marker_size = max(MIN_LINE_WIDTH, min(MAX_LINE_WIDTH, line_width))
    # Keep the original arrow spacing (2 mm arrows / 3 mm interval).
    marker_interval = max(1.0, marker_size * 1.5)

    # -----------------------------
    # LINE STYLE (repeated arrows)
    # -----------------------------
    line_symbol = QgsLineSymbol()
    while line_symbol.symbolLayerCount():
        line_symbol.deleteSymbolLayer(0)

    marker_symbol = QgsMarkerSymbol()
    while marker_symbol.symbolLayerCount():
        marker_symbol.deleteSymbolLayer(0)

    # Filled triangle, turned so it points along the drawing direction.
    triangle = QgsFilledMarkerSymbolLayer()
    triangle.setShape(QgsFilledMarkerSymbolLayer.Triangle)
    triangle.setAngle(90.0)
    triangle.setSize(marker_size)
    triangle.setSizeUnit(QgsUnitTypes.RenderMillimeters)
    triangle.setColor(line_color)
    triangle.setFillColor(line_color)
    triangle.setStrokeColor(line_color)

    marker_symbol.appendSymbolLayer(triangle)

    marker_line = QgsMarkerLineSymbolLayer()
    if hasattr(marker_line, "setPlacements"):
        marker_line.setPlacements(Qgis.MarkerLinePlacement.Interval)
    else:
        marker_line.setPlacement(QgsMarkerLineSymbolLayer.Interval)
    marker_line.setInterval(marker_interval)
    marker_line.setIntervalUnit(QgsUnitTypes.RenderMillimeters)
    if hasattr(marker_line, "setRotateSymbols"):
        marker_line.setRotateSymbols(True)
    else:
        marker_line.setRotateMarker(True)
    marker_line.setSubSymbol(marker_symbol)

    line_symbol.appendSymbolLayer(marker_line)

    layer.renderer().setSymbol(line_symbol)

    # -----------------------------
    # LABEL SETTINGS
    # -----------------------------
    settings = QgsPalLayerSettings()

    settings.enabled = True
    settings.fieldName = "Length"
    settings.isExpression = False

    # Horizontal label in the middle of the line, like the reference.
    settings.placement = QgsPalLayerSettings.Horizontal
    settings.placementFlags = (
        QgsPalLayerSettings.AboveLine |
        QgsPalLayerSettings.MapOrientation
    )
    settings.dist = 0.0
    settings.distUnits = QgsUnitTypes.RenderMillimeters
    settings.priority = 5

    # -----------------------------
    # TEXT FORMAT
    # -----------------------------
    text_format = QgsTextFormat()

    font_size = int(min(MAX_FONT_SIZE, max(MIN_FONT_SIZE, font_size)))

    font = QFont("Open Sans", font_size)
    font.setBold(True)
    text_format.setFont(font)
    text_format.setSize(font_size)
    text_format.setSizeUnit(QgsUnitTypes.RenderPoints)
    text_format.setColor(LABEL_TEXT_COLOR)

    # -----------------------------
    # BACKGROUND (yellow with shadow)
    # -----------------------------
    background = QgsTextBackgroundSettings()
    background.setEnabled(True)
    background.setType(QgsTextBackgroundSettings.ShapeRectangle)
    background.setFillColor(LABEL_BACKGROUND_COLOR)
    background.setStrokeColor(QColor(128, 128, 128))
    background.setStrokeWidth(0.0)
    background.setStrokeWidthUnit(QgsUnitTypes.RenderPoints)
    background.setSizeType(QgsTextBackgroundSettings.SizeBuffer)
    background.setSize(QSizeF(0.0, 0.0))
    text_format.setBackground(background)

    shadow = QgsTextShadowSettings()
    shadow.setEnabled(True)
    shadow.setBlurRadius(1.5)
    shadow.setBlurRadiusUnit(QgsUnitTypes.RenderMillimeters)
    shadow.setOffsetDistance(1.0)
    shadow.setOffsetUnit(QgsUnitTypes.RenderMillimeters)
    shadow.setOffsetAngle(135)
    shadow.setColor(QColor(0, 0, 0))
    shadow.setOpacity(0.7)
    text_format.setShadow(shadow)

    settings.setFormat(text_format)

    # -----------------------------
    # APPLY LABELING
    # -----------------------------
    labeling = QgsVectorLayerSimpleLabeling(settings)

    layer.setLabeling(labeling)

    layer.setLabelsEnabled(True)

    # VERY IMPORTANT
    layer.triggerRepaint()


class RulerTooltip(QLabel):
    """Small floating distance balloon that follows the map cursor."""

    def __init__(self, canvas):
        super().__init__(canvas.viewport())

        self.canvas = canvas

        self.setObjectName("rulerTooltip")
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setTextFormat(Qt.PlainText)
        self.setStyleSheet(
            "QLabel#rulerTooltip {"
            "background-color: #FFFFE1;"
            "color: #000000;"
            "border: 1px solid #000000;"
            "padding: 4px 6px;"
            "}"
        )

        font = self.font()
        font.setPointSize(9)
        self.setFont(font)

        self.hide()

    def show_text(self, text, canvas_pos):
        """Show *text* near *canvas_pos* (a point in canvas coordinates)."""

        if text != self.text():
            self.setText(text)
            self.adjustSize()

        parent = self.parentWidget()
        if parent is None:
            return

        anchor = parent.mapFromGlobal(self.canvas.mapToGlobal(canvas_pos))

        x = anchor.x() + TOOLTIP_OFFSET_X
        y = anchor.y() + TOOLTIP_OFFSET_Y

        width = self.width()
        height = self.height()
        max_x = max(2, parent.width() - width - 2)
        max_y = max(2, parent.height() - height - 2)

        # Flip to the other side when the balloon would leave the canvas.
        if x > max_x:
            x = anchor.x() - width - TOOLTIP_OFFSET_X
        if y > max_y:
            y = anchor.y() - height - TOOLTIP_OFFSET_Y

        self.move(max(2, min(x, max_x)), max(2, min(y, max_y)))
        self.show()
        self.raise_()

    def hide_tooltip(self):
        self.hide()


class ThemePopup(QDialog):
    """Asks for the dark / light / red theme of a measurement.

    Shown after every line or path. The radio of the last used theme
    is pre-selected, so a run of same-coloured lines only needs one
    click / Enter per line.
    """

    def __init__(self, last_theme=DEFAULT_THEME, parent=None):
        super().__init__(parent)

        self.setWindowTitle("Interactive Ruler - theme")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 12)
        layout.setSpacing(10)

        layout.addWidget(QLabel("Theme for this ruler:"))

        radio_row = QHBoxLayout()

        self.dark_radio = QRadioButton("Dark")
        self.dark_radio.setToolTip(
            "Black lines and arrows, best on light basemaps"
        )
        self.light_radio = QRadioButton("Light")
        self.light_radio.setToolTip(
            "White lines and arrows, best on dark basemaps "
            "such as Google Satellite"
        )
        self.red_radio = QRadioButton("Red")
        self.red_radio.setToolTip(
            "Red lines and arrows (#e31a1c), visible on most basemaps"
        )

        if last_theme == "light":
            self.light_radio.setChecked(True)
        elif last_theme == "red":
            self.red_radio.setChecked(True)
        else:
            self.dark_radio.setChecked(True)

        radio_row.addWidget(self.dark_radio)
        radio_row.addWidget(self.light_radio)
        radio_row.addWidget(self.red_radio)
        radio_row.addStretch(1)

        layout.addLayout(radio_row)

        button_row = QHBoxLayout()
        button_row.addStretch(1)

        self.ok_button = QPushButton("OK")
        self.ok_button.setObjectName("themeOkButton")
        self.ok_button.setDefault(True)
        self.ok_button.clicked.connect(self.accept)
        button_row.addWidget(self.ok_button)

        layout.addLayout(button_row)

        for radio in (self.dark_radio, self.light_radio, self.red_radio):
            if radio.isChecked():
                radio.setFocus()
                break

    def theme(self):
        if self.light_radio.isChecked():
            return "light"
        if self.red_radio.isChecked():
            return "red"
        return "dark"

    def keyPressEvent(self, event):
        # Enter and Escape both accept the pre-selected theme: the
        # ruler is already drawn, so the popup never blocks it.
        if event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Escape):
            self.accept()
            return

        super().keyPressEvent(event)


class RulerDialog(QDialog):
    """Launcher for the measuring modes, the style settings and the
    layer name.

    The sliders edit existing or future lines, depending on what is
    selected in the Layers panel when the dialog is opened:

    - with a Ruler layer (or a group holding Ruler layers) selected,
      the sliders restyle those existing lines live - each layer
      keeps its own theme, only font size and line width change,
    - with nothing selected, the sliders only set the default style
      used for lines drawn from now on.

    The dark / light / red colours are not chosen here: every finished
    line or path asks for its theme in a small popup window.
    """

    def __init__(self, layer_name=DEFAULT_LAYER_NAME, parent=None,
                 iface=None):
        super().__init__(parent)

        self.iface = iface

        self.setWindowTitle("Interactive Ruler")
        self.setModal(True)
        self.setMinimumWidth(440)
        self.setSizeGripEnabled(False)

        self._selected_mode = None

        # What the sliders restyle, from the Layers panel selection
        # (the dialog is modal, so the selection cannot change while
        # it is open).
        self.style_layers = self.selected_style_layers()

        if self.style_layers:
            _theme, font_size, line_width = layer_style(
                self.style_layers[0]
            )
        else:
            _theme, font_size, line_width = current_settings()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)
        layout.setSpacing(10)

        intro = QLabel("Choose a measurement mode:")
        layout.addWidget(intro)

        button_row = QHBoxLayout()
        button_row.setSpacing(8)

        self.straight_button = QPushButton("I-Ruler")
        self.straight_button.setObjectName("straightButton")
        self.straight_button.setToolTip("Drag to measure a straight line")
        self.straight_button.setMinimumHeight(34)
        button_row.addWidget(self.straight_button)

        self.path_button = QPushButton("I-Ruler Path")
        self.path_button.setObjectName("pathButton")
        self.path_button.setToolTip(
            "Click points to measure a path; right-click or "
            "double-click to finish"
        )
        self.path_button.setMinimumHeight(34)
        button_row.addWidget(self.path_button)

        layout.addLayout(button_row)

        # -------------------------
        # RULER STYLE
        # -------------------------
        style_box = QGroupBox("Ruler style")
        style_box.setObjectName("styleBox")

        style_layout = QVBoxLayout(style_box)
        style_layout.setSpacing(8)

        font_row = QHBoxLayout()
        font_row.addWidget(QLabel("Label font size:"))

        self.font_slider = QSlider(Qt.Horizontal)
        self.font_slider.setObjectName("fontSlider")
        self.font_slider.setToolTip(
            "Text size of the length labels, in points"
        )
        self.font_slider.setRange(MIN_FONT_SIZE, MAX_FONT_SIZE)
        self.font_slider.setValue(font_size)

        self.font_value = QLabel()
        self.font_value.setMinimumWidth(48)

        font_row.addWidget(self.font_slider)
        font_row.addWidget(self.font_value)

        style_layout.addLayout(font_row)

        width_row = QHBoxLayout()
        width_row.addWidget(QLabel("Line width:"))

        self.width_slider = QSlider(Qt.Horizontal)
        self.width_slider.setObjectName("widthSlider")
        self.width_slider.setToolTip(
            "Thickness of the arrow line, in millimetres"
        )
        # Whole millimetres are too coarse; work in tenths of a mm.
        self.width_slider.setRange(
            int(MIN_LINE_WIDTH * 10), int(MAX_LINE_WIDTH * 10)
        )
        self.width_slider.setValue(int(round(line_width * 10)))

        self.width_value = QLabel()
        self.width_value.setMinimumWidth(48)

        width_row.addWidget(self.width_slider)
        width_row.addWidget(self.width_value)

        style_layout.addLayout(width_row)

        style_hint = QLabel(
            "With a Ruler layer or group selected in the Layers "
            "panel, the sliders restyle those existing lines "
            "immediately. With nothing selected, they set the "
            "default for new measurements. The dark, light or red "
            "colours are asked in a small window after every line "
            "or path is finished; existing lines are never "
            "recoloured - redraw them to change colours."
        )
        style_hint.setWordWrap(True)
        style_hint_font = style_hint.font()
        style_hint_font.setPointSize(9)
        style_hint.setFont(style_hint_font)
        style_layout.addWidget(style_hint)

        layout.addWidget(style_box)

        # -------------------------
        # LAYER NAME
        # -------------------------
        # Type or paste the name BEFORE measuring; every finished
        # measurement is saved as its own layer with this name.
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Layer name:"))

        self.name_edit = QLineEdit(clean_layer_name(layer_name))
        self.name_edit.setObjectName("layerNameEdit")
        self.name_edit.setToolTip(
            "Name for new measurements: a group with this name is "
            "created under today's date, and every finished line or "
            "path becomes its own layer named after its result."
        )
        self.name_edit.setMinimumWidth(200)
        name_row.addWidget(self.name_edit, 1)

        layout.addLayout(name_row)

        hint = QLabel(
            "Every finished line or path becomes its own layer named "
            "after its result (for example \u201cSITE A 1.82 km\u201d) "
            "inside a group with this name under today's date.\n"
            "If you leave layer name blank, it will be named as "
            "\u201cRuler\u201d"
        )
        hint.setWordWrap(True)
        hint_font = hint.font()
        hint_font.setPointSize(9)
        hint.setFont(hint_font)
        layout.addWidget(hint)

        close_row = QHBoxLayout()
        close_row.addStretch(1)

        self.close_button = QPushButton("Close")
        self.close_button.setObjectName("closeButton")
        self.close_button.clicked.connect(self.reject)
        close_row.addWidget(self.close_button)

        layout.addLayout(close_row)

        self.straight_button.clicked.connect(lambda: self.select_mode("line"))
        self.path_button.clicked.connect(lambda: self.select_mode("path"))

        self.font_slider.valueChanged.connect(self.on_font_changed)
        self.width_slider.valueChanged.connect(self.on_width_changed)

        self.update_slider_labels()
        self.straight_button.setFocus()

    def select_mode(self, mode):
        self._selected_mode = mode
        self.accept()

    def selected_mode(self):
        return self._selected_mode

    def layer_name(self):
        """The layer name to use for measurements drawn from now on."""

        return clean_layer_name(self.name_edit.text())

    # ---------------------------------------------------------
    # STYLE SETTINGS
    # ---------------------------------------------------------
    def selected_style_layers(self):
        """Ruler layers the sliders restyle, from the panel selection.

        Selected Ruler layers plus the Ruler layers inside selected
        groups; empty when nothing Ruler-like is selected, in which
        case the sliders only change the default style.
        """

        layers = []
        seen = set()

        if self.iface is None:
            return layers

        tree_view = self.iface.layerTreeView()
        if tree_view is None:
            return layers

        root = QgsProject.instance().layerTreeRoot()

        if hasattr(tree_view, "selectedLayers"):
            for layer in tree_view.selectedLayers():
                if is_ruler_layer(layer) and layer.id() not in seen:
                    seen.add(layer.id())
                    layers.append(layer)

        if hasattr(tree_view, "selectedNodes"):
            for node in tree_view.selectedNodes():
                if isinstance(node, QgsLayerTreeGroup) and \
                        node is not root:
                    for layer in ruler_layers_in_group(node):
                        if layer.id() not in seen:
                            seen.add(layer.id())
                            layers.append(layer)

        return layers

    def update_slider_labels(self):
        self.font_value.setText("%d pt" % self.font_slider.value())
        self.width_value.setText(
            "%.1f mm" % (self.width_slider.value() / 10.0)
        )

    def apply_style_settings(self):
        """Apply the slider values to the dialog's target.

        With a Ruler layer or group selected the sliders restyle
        those existing lines right away - every layer keeps its own
        theme, only font size and line width change. With nothing
        selected they set the default style for new measurements.
        """

        font_size = self.font_slider.value()
        line_width = self.width_slider.value() / 10.0

        if self.style_layers:
            for layer in self.style_layers:
                theme, _font_size, _line_width = layer_style(layer)
                set_layer_style(layer, theme, font_size, line_width)
        else:
            theme, _font_size, _line_width = current_settings()
            save_settings(theme, font_size, line_width)

        if self.iface is not None and self.style_layers:
            tree_view = self.iface.layerTreeView()
            if tree_view is not None:
                for layer in self.style_layers:
                    tree_view.refreshLayerSymbology(layer.id())

    def on_font_changed(self, value):
        self.font_value.setText("%d pt" % value)
        self.apply_style_settings()

    def on_width_changed(self, value):
        self.width_value.setText("%.1f mm" % (value / 10.0))
        self.apply_style_settings()


class InteractiveRulerTool(QgsMapTool):

    def __init__(self, iface, mode="line",
                 layer_name=DEFAULT_LAYER_NAME):

        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.mode = mode
        self.layer_name = clean_layer_name(layer_name)

        super().__init__(self.canvas)

        # RUBBER BAND (always red, width from the line width)
        self.rubberBand = QgsRubberBand(
            self.canvas,
            QgsWkbTypes.LineGeometry
        )

        self.update_style()

        # Straight mode state
        self.start_point = None

        # Path mode state (map coordinates)
        self.path_points = []

        # Keep the ruler in the same CRS as the map canvas. Using a fixed
        # EPSG:4326 layer while the project is projected (e.g. EPSG:3857)
        # stores invalid geometry and makes measureLine() return NaN.
        self.canvas_crs = self.canvas.mapSettings().destinationCrs()

        self.layer = None
        self.layer_id = None

        # The distance area needs a valid source CRS even when the
        # project has none. EPSG:4326 is only a safe placeholder: no
        # measurement is made until a project CRS is set.
        self.distance = QgsDistanceArea()
        self.distance.setSourceCrs(
            self.canvas_crs if self.canvas_crs.isValid()
            else QgsCoordinateReferenceSystem("EPSG:4326"),
            QgsProject.instance().transformContext()
        )
        self.distance.setEllipsoid('WGS84')

        # The pending layer needs a CRS, so it is only created when the
        # project has one. Without a CRS the tool warns instead.
        if self.canvas_crs_available():
            self.layer = self.create_layer()

        # Floating live-distance balloon (created on activate()).
        self.tooltip = None

    # ---------------------------------------------------------
    # PROJECT CRS CHECK
    # ---------------------------------------------------------
    def canvas_crs_available(self):
        """True when the map canvas has a usable project CRS.

        Without one, canvas coordinates have no meaning and any
        measured length would be wrong, so the tool refuses to
        measure rather than silently showing a bogus distance.
        """

        return self.canvas.mapSettings().destinationCrs().isValid()

    def warn_missing_crs(self):
        """Tell the user why the ruler is not measuring."""

        message = (
            "Set a project CRS before measuring "
            "(Project > Properties > CRS)."
        )

        self.set_status(message)

        if self.iface is None:
            return

        try:
            bar = self.iface.messageBar()
        except AttributeError:
            bar = None

        if bar is not None:
            bar.pushWarning("Interactive Ruler", message)

    # ---------------------------------------------------------
    # TOOLTIP
    # ---------------------------------------------------------
    def activate(self):
        super().activate()

        if self.tooltip is None:
            self.tooltip = RulerTooltip(self.canvas)
            self.canvas.viewport().installEventFilter(self)

        if self.canvas_crs_available():
            self.set_status(self.status_hint())
        else:
            self.warn_missing_crs()

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Leave:
            self.hide_tooltip()

        return super().eventFilter(obj, event)

    def show_tooltip(self, text, canvas_pos):
        if self.tooltip is not None:
            self.tooltip.show_text(text, canvas_pos)

    def hide_tooltip(self):
        if self.tooltip is not None:
            self.tooltip.hide_tooltip()

    def status_hint(self):
        if self.mode == "path":
            return (
                "Click to add points; right-click or double-click "
                "to finish"
            )

        return "Drag to measure a straight line"

    def distance_label(self, dist_m):
        return "Length: %s" % self.format_length(dist_m)

    def azimuth_label(self, start_point, end_point):
        if hasattr(self.distance, "bearing"):
            # QgsDistanceArea.bearing() returns the compass azimuth in
            # radians, clockwise from north.
            azimuth = math.degrees(
                self.distance.bearing(start_point, end_point)
            ) % 360.0
        else:
            # Older QGIS versions: fall back to a planar azimuth.
            dx = end_point.x() - start_point.x()
            dy = end_point.y() - start_point.y()
            azimuth = math.degrees(math.atan2(dx, dy)) % 360.0

        return "Bearing: %.1f\u00b0" % azimuth

    # ---------------------------------------------------------
    # CREATE GROUP + SCRATCH LAYER
    # ---------------------------------------------------------
    def ensure_group(self):
        """The '<name>' group inside today's date group.

        All measurements sharing the dialog name live together in
        this group (10-Nov-2026 > SITE A).
        """

        root = QgsProject.instance().layerTreeRoot()

        day_name = QDate.currentDate().toString("dd-MMM-yyyy")

        day = root.findGroup(day_name)
        if day is None:
            day = root.insertGroup(0, day_name)

        group = day.findGroup(self.layer_name)
        if group is None:
            group = day.addGroup(self.layer_name)

        return group

    def current_layer(self):
        """The pending layer from the project, or None when gone.

        Reading it back by id also replaces a stale Python wrapper
        after the layer was deleted and re-created.
        """

        if self.layer_id is None:
            return None

        try:
            layer = QgsProject.instance().mapLayer(self.layer_id)
        except RuntimeError:
            layer = None

        self.layer = layer
        return layer

    def layer_available(self):
        """True while the pending layer still exists in the project.

        The pending layer can be deleted from outside the plugin -
        removed in the Layers panel, or the project closed - while the
        tool is still active. The Python wrapper then points at a dead
        C++ object, so every access has to be guarded by the id.
        """

        return self.current_layer() is not None

    def create_layer(self, name=None, crs=None):
        project = QgsProject.instance()

        group = self.ensure_group()

        if name is None:
            # Not named by the caller: use the name typed in the
            # dialog, numbered so it is free (Ruler, Ruler 2, ...).
            name = unique_layer_name(self.layer_name)

        layer_crs = crs if crs is not None and crs.isValid() \
            else self.canvas_crs
        crs_id = layer_crs.authid() or "EPSG:4326"

        layer = QgsVectorLayer(
            "LineString?crs=" + crs_id,
            name,
            "memory"
        )

        # Mark the layer so the style sliders can find it again later.
        layer.setCustomProperty(RULER_LAYER_PROPERTY, True)

        pr = layer.dataProvider()

        pr.addAttributes([
            QgsField("Length", QVariant.String)
        ])

        layer.updateFields()

        project.addMapLayer(layer, False)
        group.insertLayer(0, layer)

        self.apply_style(layer)

        # Remember the id: the layer itself can be deleted from
        # outside the plugin while the tool stays active.
        self.layer_id = layer.id()

        return layer

    # ---------------------------------------------------------
    # STYLE
    # ---------------------------------------------------------
    def apply_style(self, layer):
        """Stamp the current default style on *layer* and apply it."""

        set_layer_style(layer, *current_settings())

        layer.reload()

        QgsProject.instance().layerTreeRoot().findLayer(
            layer.id()
        ).setItemVisibilityChecked(True)

        self.iface.layerTreeView().refreshLayerSymbology(layer.id())

        self.canvas.refreshAllLayers()

    def ask_measurement_theme(self, layer):
        """Ask for a finished measurement's theme and style it.

        The popup opens on the theme used last time, so drawing many
        same-coloured lines is just Enter-Enter-Enter. The answer is
        saved for the next popup.
        """

        last_theme, font_size, line_width = current_settings()

        popup = ThemePopup(last_theme, self.iface.mainWindow())
        popup.exec_()

        theme = popup.theme()

        save_settings(theme, font_size, line_width)
        set_layer_style(layer, theme, font_size, line_width)

        tree_view = self.iface.layerTreeView()
        if tree_view is not None:
            tree_view.refreshLayerSymbology(layer.id())

        self.update_style()

    def update_style(self):
        """Refresh the rubber band preview from the current settings."""
        if not self.rubberBand:
            return

        _theme, _font_size, line_width = current_settings()

        self.rubberBand.setColor(RUBBER_BAND_COLOR)
        # Rough mm -> pixel mapping for the preview band only.
        self.rubberBand.setWidth(max(2, int(round(line_width))))

    # ---------------------------------------------------------
    # COORDINATE / DISPLAY HELPERS
    # ---------------------------------------------------------
    def ensure_layer_crs(self):
        """Keep the active ruler layer in the canvas' current CRS.

        A project can change CRS while QGIS is open. In that case start a
        fresh ruler layer in the new CRS instead of misplacing new geometry
        in the old one. Previously saved lines remain untouched.
        """
        canvas_crs = self.canvas.mapSettings().destinationCrs()

        if not canvas_crs.isValid():
            self.warn_missing_crs()
            return False

        if self.layer_available() and \
                self.layer.crs() == canvas_crs:
            return True

        self.canvas_crs = canvas_crs
        self.layer = self.create_layer(crs=canvas_crs)
        self.distance.setSourceCrs(
            self.layer.crs(),
            QgsProject.instance().transformContext()
        )
        self.distance.setEllipsoid('WGS84')
        return True

    def to_layer_point(self, point):

        canvas_crs = self.canvas.mapSettings().destinationCrs()
        layer_crs = self.layer.crs()

        if canvas_crs.isValid() and layer_crs.isValid() \
                and canvas_crs != layer_crs:

            transform = QgsCoordinateTransform(
                canvas_crs,
                layer_crs,
                QgsProject.instance()
            )

            return transform.transform(QgsPointXY(point))

        return QgsPointXY(point)

    def format_length(self, dist_m):
        if dist_m > 1000:
            return f"{dist_m / 1000:.2f} km"
        return f"{dist_m:.0f} m"

    def set_status(self, message):
        self.iface.mainWindow().statusBar().showMessage(message)

    def reset_rubber_band(self):
        if self.rubberBand:
            self.rubberBand.reset(QgsWkbTypes.LineGeometry)

    def draw_rubber_band(self, points):
        self.reset_rubber_band()
        for point in points:
            self.rubberBand.addPoint(QgsPointXY(point))

    def measure_layer_path(self, layer_points):
        total = 0.0
        for index in range(1, len(layer_points)):
            total += self.distance.measureLine(
                layer_points[index - 1],
                layer_points[index]
            )
        return total

    def measure_canvas_path(self, canvas_points):
        return self.measure_layer_path([
            self.to_layer_point(point) for point in canvas_points
        ])

    def pending_layer_in_name_group(self):
        """True when the empty pending layer matches the dialog name.

        The layer is created when the tool starts, but the name in
        the dialog can change while it is open. Used to move the
        pending layer into the group of the current name.
        """

        if not self.layer_available():
            return False

        root = QgsProject.instance().layerTreeRoot()

        node = root.findLayer(self.layer.id())
        if node is None:
            return False

        parent = node.parent()
        return parent is not None and \
            parent.name() == clean_layer_name(self.layer_name)

    def add_ruler_feature(self, layer_points, dist_m):
        if len(layer_points) < 2:
            return

        if not self.canvas_crs_available():
            self.warn_missing_crs()
            return

        if not self.layer_available():
            # The pending layer was deleted outside the plugin while
            # the tool stayed active; start a fresh one rather than
            # touching the dead object.
            self.layer = self.create_layer()
            self.distance.setSourceCrs(
                self.layer.crs(),
                QgsProject.instance().transformContext()
            )
            self.distance.setEllipsoid('WGS84')

        if self.layer.featureCount() > 0 or \
                not self.pending_layer_in_name_group():
            # Every measurement gets its own layer, so each saved
            # line carries the name from the dialog and can be
            # handled on its own in the Layers panel. A pending
            # layer whose group no longer matches (name changed in
            # the dialog) is replaced the same way.
            current_crs = self.layer.crs()

            if self.layer.featureCount() == 0:
                self.remove_empty_scratch_layer()

            self.layer = self.create_layer(crs=current_crs)
            self.distance.setSourceCrs(
                self.layer.crs(),
                QgsProject.instance().transformContext()
            )
            self.distance.setEllipsoid('WGS84')

        # Name the finished layer after its result, so the group
        # reads like a list of results: SITE A > SITE A 1.82 km.
        self.layer.setName(unique_layer_name(
            "%s %s" % (self.layer_name, self.format_length(dist_m))
        ))

        feat = QgsFeature(self.layer.fields())

        feat.setGeometry(
            QgsGeometry.fromPolylineXY(layer_points)
        )

        feat["Length"] = self.format_length(dist_m)

        self.layer.dataProvider().addFeature(feat)

        self.layer.updateExtents()

        self.layer.setLabelsEnabled(True)

        # Every finished measurement asks for its theme; the last
        # choice is pre-selected next time. Existing layers are never
        # recoloured - redraw a line to change its colour.
        self.ask_measurement_theme(self.layer)

        self.layer.triggerRepaint()

        self.canvas.refreshAllLayers()

    def clear_measurement(self):
        self.start_point = None
        self.path_points = []
        self.hide_tooltip()
        self.reset_rubber_band()
        self.set_status(self.status_hint())

    # ---------------------------------------------------------
    # PATH MODE HELPERS
    # ---------------------------------------------------------
    def add_path_point(self, point, canvas_pos=None):

        if not self.canvas_crs_available():
            self.warn_missing_crs()
            return

        # If the project CRS changed in the middle of a path, start over
        # rather than mixing coordinates from two different CRSs. The
        # same applies when the pending layer disappeared underneath us.
        canvas_crs = self.canvas.mapSettings().destinationCrs()
        if self.path_points and (
                not self.layer_available()
                or canvas_crs != self.layer.crs()):
            self.clear_measurement()

        if not self.path_points and not self.ensure_layer_crs():
            return

        point = QgsPointXY(point)

        if self.path_points and self.path_points[-1] == point:
            return

        self.path_points.append(point)

        self.draw_rubber_band(self.path_points)

        if len(self.path_points) >= 2 and canvas_pos is not None:
            dist_m = self.measure_canvas_path(self.path_points)
            self.show_tooltip(self.distance_label(dist_m), canvas_pos)
        else:
            self.hide_tooltip()

        self.canvas.refresh()

    def update_path_preview(self, end_point, canvas_pos):
        if not self.path_points:
            self.hide_tooltip()
            return

        preview = self.path_points + [QgsPointXY(end_point)]

        self.draw_rubber_band(preview)

        dist_m = self.measure_canvas_path(preview)
        self.show_tooltip(self.distance_label(dist_m), canvas_pos)

        self.canvas.refresh()

    def finish_path(self):
        self.hide_tooltip()

        if len(self.path_points) < 2:
            self.clear_measurement()
            return

        layer_points = [
            self.to_layer_point(point) for point in self.path_points
        ]

        dist_m = self.measure_layer_path(layer_points)

        self.add_ruler_feature(layer_points, dist_m)

        self.clear_measurement()

    # ---------------------------------------------------------
    # MOUSE PRESS
    # ---------------------------------------------------------
    def canvasPressEvent(self, event):

        if not self.canvas_crs_available():
            self.warn_missing_crs()
            return

        if event.button() == Qt.LeftButton:

            if self.mode == "path":
                self.add_path_point(
                    self.toMapCoordinates(event.pos()), event.pos()
                )
                return

            self.ensure_layer_crs()

            self.start_point = self.toMapCoordinates(event.pos())

            # clear any stale rubber band from a previous drag
            self.reset_rubber_band()

        elif event.button() == Qt.RightButton:

            if self.mode == "path" and len(self.path_points) >= 2:
                self.finish_path()
                return

            self.clear_measurement()

    # ---------------------------------------------------------
    # LIVE DRAGGING / PATH PREVIEW
    # ---------------------------------------------------------
    def canvasMoveEvent(self, event):

        if not self.canvas_crs_available():
            self.hide_tooltip()
            return

        if self.mode == "path":
            if self.path_points:
                self.update_path_preview(
                    self.toMapCoordinates(event.pos()), event.pos()
                )
            else:
                self.hide_tooltip()
            return

        if not self.start_point:
            self.hide_tooltip()
            return

        end_point = self.toMapCoordinates(event.pos())

        start_layer = self.to_layer_point(self.start_point)
        end_layer = self.to_layer_point(end_point)

        # RUBBER BAND
        self.draw_rubber_band([self.start_point, end_point])

        # DISTANCE (measured in the ruler layer CRS)
        dist_m = self.distance.measureLine(start_layer, end_layer)

        # LIVE DISPLAY NEXT TO THE CURSOR
        if dist_m > 0:
            text = "%s\n%s" % (
                self.azimuth_label(start_layer, end_layer),
                self.distance_label(dist_m)
            )
        else:
            text = self.distance_label(dist_m)

        self.show_tooltip(text, event.pos())

        self.canvas.refresh()

    # ---------------------------------------------------------
    # FINISH STRAIGHT LINE / PATH CLICK
    # ---------------------------------------------------------
    def canvasReleaseEvent(self, event):

        if not self.canvas_crs_available():
            self.hide_tooltip()
            return

        if self.mode == "path":
            return

        if event.button() != Qt.LeftButton:
            return

        self.hide_tooltip()

        if not self.start_point:
            return

        end_point = self.toMapCoordinates(event.pos())

        start_layer = self.to_layer_point(self.start_point)
        end_layer = self.to_layer_point(end_point)

        dist_m = self.distance.measureLine(
            start_layer,
            end_layer
        )

        self.add_ruler_feature([start_layer, end_layer], dist_m)

        self.clear_measurement()

    def canvasDoubleClickEvent(self, event):
        if self.mode == "path":
            self.finish_path()
            return

        super().canvasDoubleClickEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.clear_measurement()
            return

        super().keyPressEvent(event)

    def deactivate(self):
        self.clear_measurement()
        self.set_status("")

        if self.tooltip is not None:
            self.canvas.viewport().removeEventFilter(self)
            self.tooltip.deleteLater()
            self.tooltip = None

        # Leave nothing behind when no measurement was finished.
        self.remove_empty_scratch_layer()

        super().deactivate()

    def remove_empty_scratch_layer(self):
        """Remove the pending layer and now-empty groups around it.

        Activating the tool creates a pending layer at once (the
        CRS must be known before the first click). When the tool is
        switched off before anything was measured, that layer - and
        its group, when now empty - is dropped again.

        The layer may already be gone (removed in the Layers panel,
        project closed): the id is tracked, so the cleanup never
        touches a deleted C++ object.
        """

        layer_id = self.layer_id
        self.layer = None
        self.layer_id = None

        if layer_id is None:
            return

        project = QgsProject.instance()

        # A layer that holds a finished measurement stays.
        layer = project.mapLayer(layer_id)
        if layer is not None:
            try:
                if layer.featureCount() > 0:
                    self.layer = layer
                    self.layer_id = layer_id
                    return
            except RuntimeError:
                pass

        root = project.layerTreeRoot()

        node = root.findLayer(layer_id)
        parent = node.parent() if node is not None else None

        if node is not None and parent is not None:
            parent.removeChildNode(node)

        project.removeMapLayer(layer_id)

        # When the layer was deleted outside the plugin its tree node
        # is already gone; look the (now empty) name group up by name
        # so it does not linger.
        if parent is None:
            day_name = QDate.currentDate().toString("dd-MMM-yyyy")
            day = root.findGroup(day_name)
            if day is not None:
                parent = day.findGroup(self.layer_name)

        # Drop groups that only held the pending layer (the name
        # group, then the day group when it is empty too).
        group = parent
        while group is not None and group is not root and \
                isinstance(group, QgsLayerTreeGroup) and \
                not group.children():
            grand = group.parent()
            if grand is None:
                break
            grand.removeChildNode(group)
            group = grand


# ---------------------------------------------------------
# MAIN PLUGIN
# ---------------------------------------------------------
class InteractiveRulerPlugin:

    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.tool = None
        self.layer_name = clean_layer_name(QgsSettings().value(
            LAYER_NAME_SETTINGS_KEY, DEFAULT_LAYER_NAME, type=str
        ))

    def plugin_icon(self):
        """The bundled SVG icon, as a QIcon."""

        import os

        icon_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), PLUGIN_ICON
        )

        if os.path.exists(icon_path):
            return QIcon(icon_path)

        return QIcon()

    def initGui(self):

        self.action = QAction(self.plugin_icon(), "I-Ruler",
                              self.iface.mainWindow())
        self.action.setToolTip("Open the Interactive Ruler dialog")
        self.action.triggered.connect(self.open_dialog)

        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu("&Interactive Ruler", self.action)

    def unload(self):

        if self.action:
            self.iface.removeToolBarIcon(self.action)
            self.iface.removePluginMenu("&Interactive Ruler", self.action)

    def create_dialog(self):
        return RulerDialog(
            self.layer_name, self.iface.mainWindow(), self.iface
        )

    def save_layer_name(self):
        QgsSettings().setValue(LAYER_NAME_SETTINGS_KEY, self.layer_name)

    def open_dialog(self):
        dialog = self.create_dialog()
        result = dialog.exec_()

        self.layer_name = dialog.layer_name()
        self.save_layer_name()

        # Keep the preview of a still active ruler tool in sync with
        # any font size / line width change made in the dialog, and
        # let new measurements pick up the name just typed or pasted.
        if self.tool is not None:
            self.tool.update_style()
            self.tool.layer_name = self.layer_name

        if result != QDialog.Accepted:
            return

        mode = dialog.selected_mode()
        if mode == "path":
            self.activate_path_tool()
        elif mode == "line":
            self.activate_tool()

    def activate_tool(self):
        self.tool = InteractiveRulerTool(
            self.iface, mode="line", layer_name=self.layer_name
        )
        self.iface.mapCanvas().setMapTool(self.tool)

    def activate_path_tool(self):
        self.tool = InteractiveRulerTool(
            self.iface, mode="path", layer_name=self.layer_name
        )
        self.iface.mapCanvas().setMapTool(self.tool)
