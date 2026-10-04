from __future__ import annotations

import re

_MATH_FRAGMENT_RE = re.compile(r"\$(.+?)\$|\\\((.+?)\\\)")
_STYLE_COMMAND_RE = re.compile(
    r"\\(?:mathrm|mathbf|mathit|mathsf|mathtt|textrm|textbf|textit|text)\{([^{}]*)\}"
)
_FRAC_RE = re.compile(r"\\frac\{([^{}]+)\}\{([^{}]+)\}")
_SQRT_RE = re.compile(r"\\sqrt\{([^{}]+)\}")
_SUPERSCRIPT_RE = re.compile(r"\^(?:\{([^{}]+)\}|([A-Za-z0-9+\-=()]))")
_SUBSCRIPT_RE = re.compile(r"_(?:\{([^{}]+)\}|([A-Za-z0-9+\-=()]))")
_UNKNOWN_COMMAND_RE = re.compile(r"\\([A-Za-z]+)")

_SUPERSCRIPT = str.maketrans(
    {
        "0": "⁰",
        "1": "¹",
        "2": "²",
        "3": "³",
        "4": "⁴",
        "5": "⁵",
        "6": "⁶",
        "7": "⁷",
        "8": "⁸",
        "9": "⁹",
        "+": "⁺",
        "-": "⁻",
        "=": "⁼",
        "(": "⁽",
        ")": "⁾",
        "n": "ⁿ",
        "i": "ⁱ",
    }
)
_SUBSCRIPT = str.maketrans(
    {
        "0": "₀",
        "1": "₁",
        "2": "₂",
        "3": "₃",
        "4": "₄",
        "5": "₅",
        "6": "₆",
        "7": "₇",
        "8": "₈",
        "9": "₉",
        "+": "₊",
        "-": "₋",
        "=": "₌",
        "(": "₍",
        ")": "₎",
        "a": "ₐ",
        "e": "ₑ",
        "h": "ₕ",
        "i": "ᵢ",
        "j": "ⱼ",
        "k": "ₖ",
        "l": "ₗ",
        "m": "ₘ",
        "n": "ₙ",
        "o": "ₒ",
        "p": "ₚ",
        "r": "ᵣ",
        "s": "ₛ",
        "t": "ₜ",
        "u": "ᵤ",
        "v": "ᵥ",
        "x": "ₓ",
    }
)

_COMMANDS = {
    r"\alpha": "α",
    r"\beta": "β",
    r"\gamma": "γ",
    r"\delta": "δ",
    r"\epsilon": "ε",
    r"\varepsilon": "ε",
    r"\zeta": "ζ",
    r"\eta": "η",
    r"\theta": "θ",
    r"\vartheta": "ϑ",
    r"\iota": "ι",
    r"\kappa": "κ",
    r"\lambda": "λ",
    r"\mu": "μ",
    r"\nu": "ν",
    r"\xi": "ξ",
    r"\pi": "π",
    r"\rho": "ρ",
    r"\sigma": "σ",
    r"\tau": "τ",
    r"\upsilon": "υ",
    r"\phi": "φ",
    r"\varphi": "φ",
    r"\chi": "χ",
    r"\psi": "ψ",
    r"\omega": "ω",
    r"\Gamma": "Γ",
    r"\Delta": "Δ",
    r"\Theta": "Θ",
    r"\Lambda": "Λ",
    r"\Xi": "Ξ",
    r"\Pi": "Π",
    r"\Sigma": "Σ",
    r"\Phi": "Φ",
    r"\Psi": "Ψ",
    r"\Omega": "Ω",
    r"\times": "×",
    r"\cdot": "·",
    r"\pm": "±",
    r"\mp": "∓",
    r"\leq": "≤",
    r"\le": "≤",
    r"\geq": "≥",
    r"\ge": "≥",
    r"\neq": "≠",
    r"\approx": "≈",
    r"\sim": "∼",
    r"\infty": "∞",
    r"\rightarrow": "→",
    r"\to": "→",
    r"\leftarrow": "←",
    r"\leftrightarrow": "↔",
    r"\in": "∈",
    r"\notin": "∉",
    r"\subset": "⊂",
    r"\subseteq": "⊆",
}


def format_paper_title(title: str) -> str:
    """Return a readable plain-text title for Flet controls.

    arXiv titles occasionally contain inline LaTeX (for example ``G$^2$PTQ``).
    Flet ``Text`` does not typeset LaTeX, so raw ``$...$`` markup leaks into the
    UI.  Keep the stored title untouched and normalize only the presentation
    string into Unicode where the conversion is unambiguous.  Unknown constructs
    degrade to readable plain text rather than pretending to be a full TeX
    renderer.
    """

    if not title:
        return ""

    def replace_fragment(match: re.Match[str]) -> str:
        fragment = match.group(1) if match.group(1) is not None else match.group(2)
        return _normalize_math_fragment(fragment or "")

    text = _MATH_FRAGMENT_RE.sub(replace_fragment, title)
    # arXiv metadata can also contain escaped punctuation outside math spans.
    text = (
        text.replace(r"\%", "%")
        .replace(r"\&", "&")
        .replace(r"\_", "_")
        .replace(r"\#", "#")
    )
    return " ".join(text.split())


def _normalize_math_fragment(fragment: str) -> str:
    text = fragment.strip()
    if not text:
        return ""

    # Remove common sizing directives that do not carry textual meaning.
    for token in (r"\left", r"\right", r"\!", r"\,", r"\;", r"\:"):
        text = text.replace(token, "" if token in {r"\left", r"\right", r"\!"} else " ")

    # Flatten common style wrappers. Repeat so nested one-level wrappers settle.
    previous = None
    while previous != text:
        previous = text
        text = _STYLE_COMMAND_RE.sub(r"\1", text)
        text = _FRAC_RE.sub(r"\1/\2", text)
        text = _SQRT_RE.sub(r"√\1", text)

    for command, symbol in _COMMANDS.items():
        text = text.replace(command, symbol)

    text = _SUPERSCRIPT_RE.sub(lambda match: _script_value(match, _SUPERSCRIPT, "^"), text)
    text = _SUBSCRIPT_RE.sub(lambda match: _script_value(match, _SUBSCRIPT, "_"), text)

    # Preserve unknown command names as readable words instead of exposing a
    # backslash. This is deliberately conservative and is not a TeX parser.
    text = _UNKNOWN_COMMAND_RE.sub(r"\1", text)
    text = text.replace("{", "").replace("}", "")
    text = text.replace("~", " ")
    return " ".join(text.split())


def _script_value(match: re.Match[str], table: dict[int, str], prefix: str) -> str:
    raw = match.group(1) if match.group(1) is not None else match.group(2)
    translated = raw.translate(table)
    # ``translate`` leaves unsupported characters unchanged. Only use Unicode
    # script notation when every character changed into a script glyph.
    if raw and translated != raw and all(
        original != converted for original, converted in zip(raw, translated, strict=True)
    ):
        return translated
    return f"{prefix}{raw}"
