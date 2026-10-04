"""Visual design tokens for the desktop research workspace.

The visual system borrows the calm hierarchy and generous legibility of modern
macOS/iPad research tools while remaining a Windows-first Flet application.
Enabled controls should never resemble disabled grey controls; neutral grey is
reserved for secondary text, dividers and truly disabled states.
"""

APP_BG = "#F6F8FB"
SURFACE = "#FFFFFF"
SIDEBAR_BG = "#F8FBFD"
SURFACE_SUBTLE = "#F7FAFD"
SURFACE_HOVER = "#EEF4F9"
TEXT_PRIMARY = "#172230"
TEXT_SECONDARY = "#405266"
TEXT_TERTIARY = "#687A8D"
DIVIDER = "#E3E9F0"
DIVIDER_STRONG = "#C9D4DF"

# Main accent: a restrained ink / research blue.
PRIMARY = "#315F8C"
PRIMARY_DARK = "#244A70"
PRIMARY_SOFT = "#EDF4FA"
PRIMARY_BORDER = "#B8CEE1"

TEAL = "#2F7A78"
TEAL_SOFT = "#EAF5F4"
PURPLE = "#6750A4"
PURPLE_SOFT = "#F2EEFA"
SUCCESS = "#2F7658"
WARNING = "#9A6200"
DANGER = "#B42318"
DANGER_SOFT = "#FFF1F0"

DISABLED_BG = "#FFFFFF"
DISABLED_TEXT = "#A4ADB8"

# Desktop typography. 12 px is the minimum for secondary labels; normal reading
# text starts at 14 px so a 2K Windows desktop does not feel visually undersized.
FONT_CAPTION = 12
FONT_META = 13
FONT_BODY = 14
FONT_CONTROL = 13
FONT_SECTION = 15
FONT_CARD_TITLE = 17
FONT_DETAIL_TITLE = 20
FONT_PAGE_TITLE = 28

PAGE_PADDING = 22
SECTION_GAP = 16
CARD_RADIUS = 14
CONTROL_RADIUS = 10
SIDEBAR_WIDTH = 216
INSPECTOR_WIDTH = 408
MAX_CENTER_WIDTH = 1040
