# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337

from __future__ import annotations

from dataclasses import dataclass
from html import escape


@dataclass(frozen=True)
class IconSpec:
    name: str
    view_box: str
    shapes: str
    source_url: str
    sha256: str


# Source: https://raw.githubusercontent.com/carbon-design-system/carbon/main/packages/icons/src/svg/32/home.svg
# SHA-256: 9a36ce0907fc6e5530c6735201bc85b08be950ca91255f173d7507faf9c599c0
# Source: https://raw.githubusercontent.com/carbon-design-system/carbon/main/packages/icons/src/svg/32/download.svg
# SHA-256: 98e30ad6b5dacf01f69c494a961957cee4a5fa05c54977b3f9dae230e46a0df5
# Source: https://raw.githubusercontent.com/carbon-design-system/carbon/main/packages/icons/src/svg/32/chart--column.svg
# SHA-256: 8a1cc17173ed021e5f0af830f1d8a3d80331d9c90e665571bc4baa0972fdcdac
# Source: https://raw.githubusercontent.com/carbon-design-system/carbon/main/packages/icons/src/svg/32/settings.svg
# SHA-256: fa8f42440fb69cae30d6223e3e1d06ed84e0282b4d5f593c2385ca624d4ed940
# Source: https://raw.githubusercontent.com/carbon-design-system/carbon/main/packages/icons/src/svg/32/menu.svg
# SHA-256: 54571562579bd3dac158ad268626c9592a315c5a97dfa727aff40e96b398a0f7
# Source: https://raw.githubusercontent.com/carbon-design-system/carbon/main/packages/icons/src/svg/32/close.svg
# SHA-256: 2dcd41fffb7be7ed16dd41017800a07c6163d9200aef261ab820ce93a14e1f91
# Source: https://raw.githubusercontent.com/carbon-design-system/carbon/main/packages/icons/src/svg/32/light.svg
# SHA-256: 84110ac04c8f97b34699c9ccf1f196b75e2cc7a88eea465da49d5f331bb21852
# Source: https://raw.githubusercontent.com/carbon-design-system/carbon/main/packages/icons/src/svg/32/moon.svg
# SHA-256: ad179db2f5f3ecc92954d771de42ed63b24f6b2e1d729dd043037e27be8454f9
# Source: https://raw.githubusercontent.com/carbon-design-system/carbon/main/packages/icons/src/svg/32/notification.svg
# SHA-256: f62c8620681f36db7753b8c9dbd25185f251e8b2ec904c32d7e55b072e24582b
# Source: https://raw.githubusercontent.com/carbon-design-system/carbon/main/packages/icons/src/svg/32/user.svg
# SHA-256: ad4d00568be05c4d395846c50bb2c1f66e4ec695cd53c45663d9b91bb0478799
# Source: https://raw.githubusercontent.com/carbon-design-system/carbon/main/packages/icons/src/svg/32/logout.svg
# SHA-256: b3a6ddc6080545cd04f859ecf961f52f32a9a3eaaf30d9461b3b8660cdcad08f
# Source: https://raw.githubusercontent.com/carbon-design-system/carbon/main/packages/icons/src/svg/32/security.svg
# SHA-256: 245eb43197a9264275d4b2d08bd6fdc20563541e2bb76154cbe277f520985150
# Source: https://raw.githubusercontent.com/carbon-design-system/carbon/main/packages/icons/src/svg/32/trash-can.svg
# SHA-256: 9c83b21c04247581554bc3a5e9fa5ef46b632aa8c507d4f33a3dabee2c5061cc
# Source: https://raw.githubusercontent.com/carbon-design-system/carbon/main/packages/icons/src/svg/32/renew.svg
# SHA-256: 31026edcb1d8295fcc3452498833f0c38639eb97ff92a5b29733648b0b831d88
# Source: https://raw.githubusercontent.com/carbon-design-system/carbon/main/packages/icons/src/svg/32/unlocked.svg
# SHA-256: d8797e1ee9240a3ee5d0d124fee2979342c651a1c577617a964aa30cc61cd9df
# Source: https://raw.githubusercontent.com/carbon-design-system/carbon/main/packages/icons/src/svg/32/arrow--up.svg
# SHA-256: 5ef42a6b8f7d8c23fb9ee12d8165edb486475cd2d9c5ca7b75633cc5fe1fb22b
# Source: https://raw.githubusercontent.com/carbon-design-system/carbon/main/packages/icons/src/svg/32/arrow--down.svg
# SHA-256: 1f5817e9a2aa65dd4fefb31775e9e0cf5470f7fa51d9c90e0605505a0117ffaf
ICONS: dict[str, IconSpec] = {
    "home": IconSpec(
        name="home",
        view_box="0 0 32 32",
        shapes="<path d='M16.6123,2.2138a1.01,1.01,0,0,0-1.2427,0L1,13.4194l1.2427,1.5717L4,13.6209V26a2.0041,2.0041,0,0,0,2,2H26a2.0037,2.0037,0,0,0,2-2V13.63L29.7573,15,31,13.4282ZM18,26H14V18h4Zm2,0V18a2.0023,2.0023,0,0,0-2-2H14a2.002,2.002,0,0,0-2,2v8H6V12.0615l10-7.79,10,7.8005V26Z'/>",
        source_url="https://raw.githubusercontent.com/carbon-design-system/carbon/main/packages/icons/src/svg/32/home.svg",
        sha256="9a36ce0907fc6e5530c6735201bc85b08be950ca91255f173d7507faf9c599c0",
    ),
    "download": IconSpec(
        name="download",
        view_box="0 0 32 32",
        shapes="<path d='M26,24v4H6V24H4v4H4a2,2,0,0,0,2,2H26a2,2,0,0,0,2-2h0V24Z'/><polygon points='26 14 24.59 12.59 17 20.17 17 2 15 2 15 20.17 7.41 12.59 6 14 16 24 26 14' />",
        source_url="https://raw.githubusercontent.com/carbon-design-system/carbon/main/packages/icons/src/svg/32/download.svg",
        sha256="98e30ad6b5dacf01f69c494a961957cee4a5fa05c54977b3f9dae230e46a0df5",
    ),
    "chart--column": IconSpec(
        name="chart--column",
        view_box="0 0 32 32",
        shapes="<path d='M27,28V6H19V28H15V14H7V28H4V2H2V28a2,2,0,0,0,2,2H30V28ZM13,28H9V16h4Zm12,0H21V8h4Z' />",
        source_url="https://raw.githubusercontent.com/carbon-design-system/carbon/main/packages/icons/src/svg/32/chart--column.svg",
        sha256="8a1cc17173ed021e5f0af830f1d8a3d80331d9c90e665571bc4baa0972fdcdac",
    ),
    "settings": IconSpec(
        name="settings",
        view_box="0 0 32 32",
        shapes="<path d='M27,16.76c0-.25,0-.5,0-.76s0-.51,0-.77l1.92-1.68A2,2,0,0,0,29.3,11L26.94,7a2,2,0,0,0-1.73-1,2,2,0,0,0-.64.1l-2.43.82a11.35,11.35,0,0,0-1.31-.75l-.51-2.52a2,2,0,0,0-2-1.61H13.64a2,2,0,0,0-2,1.61l-.51,2.52a11.48,11.48,0,0,0-1.32.75L7.43,6.06A2,2,0,0,0,6.79,6,2,2,0,0,0,5.06,7L2.7,11a2,2,0,0,0,.41,2.51L5,15.24c0,.25,0,.5,0,.76s0,.51,0,.77L3.11,18.45A2,2,0,0,0,2.7,21L5.06,25a2,2,0,0,0,1.73,1,2,2,0,0,0,.64-.1l2.43-.82a11.35,11.35,0,0,0,1.31.75l.51,2.52a2,2,0,0,0,2,1.61h4.72a2,2,0,0,0,2-1.61l.51-2.52a11.48,11.48,0,0,0,1.32-.75l2.42.82a2,2,0,0,0,.64.1,2,2,0,0,0,1.73-1L29.3,21a2,2,0,0,0-.41-2.51ZM25.21,24l-3.43-1.16a8.86,8.86,0,0,1-2.71,1.57L18.36,28H13.64l-.71-3.55a9.36,9.36,0,0,1-2.7-1.57L6.79,24,4.43,20l2.72-2.4a8.9,8.9,0,0,1,0-3.13L4.43,12,6.79,8l3.43,1.16a8.86,8.86,0,0,1,2.71-1.57L13.64,4h4.72l.71,3.55a9.36,9.36,0,0,1,2.7,1.57L25.21,8,27.57,12l-2.72,2.4a8.9,8.9,0,0,1,0,3.13L27.57,20Z' /><path d='M16,22a6,6,0,1,1,6-6A5.94,5.94,0,0,1,16,22Zm0-10a3.91,3.91,0,0,0-4,4,3.91,3.91,0,0,0,4,4,3.91,3.91,0,0,0,4-4A3.91,3.91,0,0,0,16,12Z' />",
        source_url="https://raw.githubusercontent.com/carbon-design-system/carbon/main/packages/icons/src/svg/32/settings.svg",
        sha256="fa8f42440fb69cae30d6223e3e1d06ed84e0282b4d5f593c2385ca624d4ed940",
    ),
    "menu": IconSpec(
        name="menu",
        view_box="0 0 32 32",
        shapes="<rect x='4' y='6' width='24' height='2' /><rect x='4' y='24' width='24' height='2' /><rect x='4' y='12' width='24' height='2' /><rect x='4' y='18' width='24' height='2' />",
        source_url="https://raw.githubusercontent.com/carbon-design-system/carbon/main/packages/icons/src/svg/32/menu.svg",
        sha256="54571562579bd3dac158ad268626c9592a315c5a97dfa727aff40e96b398a0f7",
    ),
    "close": IconSpec(
        name="close",
        view_box="0 0 32 32",
        shapes="<polygon points='17.4141 16 24 9.4141 22.5859 8 16 14.5859 9.4143 8 8 9.4141 14.5859 16 8 22.5859 9.4143 24 16 17.4141 22.5859 24 24 22.5859 17.4141 16' />",
        source_url="https://raw.githubusercontent.com/carbon-design-system/carbon/main/packages/icons/src/svg/32/close.svg",
        sha256="2dcd41fffb7be7ed16dd41017800a07c6163d9200aef261ab820ce93a14e1f91",
    ),
    "light": IconSpec(
        name="light",
        view_box="0 0 32 32",
        shapes="<rect x='15' y='2' width='2' height='5' /><rect x='21.6675' y='6.8536' width='4.958' height='1.9998' transform='translate(1.5191 19.3744) rotate(-45)' /><rect x='25' y='15' width='5' height='2' /><rect x='23.1466' y='21.6675' width='1.9998' height='4.958' transform='translate(-10.0018 24.1465) rotate(-45)' /><rect x='15' y='25' width='2' height='5' /><rect x='5.3745' y='23.1466' width='4.958' height='1.9998' transform='translate(-14.7739 12.6256) rotate(-45)' /><rect x='2' y='15' width='5' height='2' /><rect x='6.8536' y='5.3745' width='1.9998' height='4.958' transform='translate(-3.253 7.8535) rotate(-45)' /><path d='M16,12a4,4,0,1,1-4,4,4.0045,4.0045,0,0,1,4-4m0-2a6,6,0,1,0,6,6,6,6,0,0,0-6-6Z' />",
        source_url="https://raw.githubusercontent.com/carbon-design-system/carbon/main/packages/icons/src/svg/32/light.svg",
        sha256="84110ac04c8f97b34699c9ccf1f196b75e2cc7a88eea465da49d5f331bb21852",
    ),
    "moon": IconSpec(
        name="moon",
        view_box="0 0 32 32",
        shapes="<path d='M13.5025,5.4136A15.0755,15.0755,0,0,0,25.096,23.6082a11.1134,11.1134,0,0,1-7.9749,3.3893c-.1385,0-.2782.0051-.4178,0A11.0944,11.0944,0,0,1,13.5025,5.4136M14.98,3a1.0024,1.0024,0,0,0-.1746.0156A13.0959,13.0959,0,0,0,16.63,28.9973c.1641.006.3282,0,.4909,0a13.0724,13.0724,0,0,0,10.702-5.5556,1.0094,1.0094,0,0,0-.7833-1.5644A13.08,13.08,0,0,1,15.8892,4.38,1.0149,1.0149,0,0,0,14.98,3Z' />",
        source_url="https://raw.githubusercontent.com/carbon-design-system/carbon/main/packages/icons/src/svg/32/moon.svg",
        sha256="ad179db2f5f3ecc92954d771de42ed63b24f6b2e1d729dd043037e27be8454f9",
    ),
    "notification": IconSpec(
        name="notification",
        view_box="0 0 32 32",
        shapes="<path d='M28.7071,19.293,26,16.5859V13a10.0136,10.0136,0,0,0-9-9.9492V1H15V3.0508A10.0136,10.0136,0,0,0,6,13v3.5859L3.2929,19.293A1,1,0,0,0,3,20v3a1,1,0,0,0,1,1h7v.7768a5.152,5.152,0,0,0,4.5,5.1987A5.0057,5.0057,0,0,0,21,25V24h7a1,1,0,0,0,1-1V20A1,1,0,0,0,28.7071,19.293ZM19,25a3,3,0,0,1-6,0V24h6Zm8-3H5V20.4141L7.707,17.707A1,1,0,0,0,8,17V13a8,8,0,0,1,16,0v4a1,1,0,0,0,.293.707L27,20.4141Z' />",
        source_url="https://raw.githubusercontent.com/carbon-design-system/carbon/main/packages/icons/src/svg/32/notification.svg",
        sha256="f62c8620681f36db7753b8c9dbd25185f251e8b2ec904c32d7e55b072e24582b",
    ),
    "user": IconSpec(
        name="user",
        view_box="0 0 32 32",
        shapes="<path d='M16,4a5,5,0,1,1-5,5,5,5,0,0,1,5-5m0-2a7,7,0,1,0,7,7A7,7,0,0,0,16,2Z' /><path d='M26,30H24V25a5,5,0,0,0-5-5H13a5,5,0,0,0-5,5v5H6V25a7,7,0,0,1,7-7h6a7,7,0,0,1,7,7Z' />",
        source_url="https://raw.githubusercontent.com/carbon-design-system/carbon/main/packages/icons/src/svg/32/user.svg",
        sha256="ad4d00568be05c4d395846c50bb2c1f66e4ec695cd53c45663d9b91bb0478799",
    ),
    "logout": IconSpec(
        name="logout",
        view_box="0 0 32 32",
        shapes="<path d='M6,30H18a2.0023,2.0023,0,0,0,2-2V25H18v3H6V4H18V7h2V4a2.0023,2.0023,0,0,0-2-2H6A2.0023,2.0023,0,0,0,4,4V28A2.0023,2.0023,0,0,0,6,30Z' /><polygon points='20.586 20.586 24.172 17 10 17 10 15 24.172 15 20.586 11.414 22 10 28 16 22 22 20.586 20.586' />",
        source_url="https://raw.githubusercontent.com/carbon-design-system/carbon/main/packages/icons/src/svg/32/logout.svg",
        sha256="b3a6ddc6080545cd04f859ecf961f52f32a9a3eaaf30d9461b3b8660cdcad08f",
    ),
    "security": IconSpec(
        name="security",
        view_box="0 0 32 32",
        shapes="<polygon points='14 16.59 11.41 14 10 15.41 14 19.41 22 11.41 20.59 10 14 16.59' /><path d='M16,30,9.8242,26.7071A10.9818,10.9818,0,0,1,4,17V4A2.0021,2.0021,0,0,1,6,2H26a2.0021,2.0021,0,0,1,2,2V17a10.9818,10.9818,0,0,1-5.8242,9.7071ZM6,4V17a8.9852,8.9852,0,0,0,4.7656,7.9423L16,27.7333l5.2344-2.791A8.9852,8.9852,0,0,0,26,17V4Z' />",
        source_url="https://raw.githubusercontent.com/carbon-design-system/carbon/main/packages/icons/src/svg/32/security.svg",
        sha256="245eb43197a9264275d4b2d08bd6fdc20563541e2bb76154cbe277f520985150",
    ),
    "trash-can": IconSpec(
        name="trash-can",
        view_box="0 0 32 32",
        shapes="<rect x='12' y='12' width='2' height='12'/><rect x='18' y='12' width='2' height='12'/><path d='M4,6V8H6V28a2,2,0,0,0,2,2H24a2,2,0,0,0,2-2V8h2V6ZM8,28V8H24V28Z'/><rect x='12' y='2' width='8' height='2'/>",
        source_url="https://raw.githubusercontent.com/carbon-design-system/carbon/main/packages/icons/src/svg/32/trash-can.svg",
        sha256="9c83b21c04247581554bc3a5e9fa5ef46b632aa8c507d4f33a3dabee2c5061cc",
    ),
    "renew": IconSpec(
        name="renew",
        view_box="0 0 32 32",
        shapes="<path d='M12,10H6.78A11,11,0,0,1,27,16h2A13,13,0,0,0,6,7.68V4H4v8h8Z'/><path d='M20,22h5.22A11,11,0,0,1,5,16H3a13,13,0,0,0,23,8.32V28h2V20H20Z'/>",
        source_url="https://raw.githubusercontent.com/carbon-design-system/carbon/main/packages/icons/src/svg/32/renew.svg",
        sha256="31026edcb1d8295fcc3452498833f0c38639eb97ff92a5b29733648b0b831d88",
    ),
    "unlocked": IconSpec(
        name="unlocked",
        view_box="0 0 32 32",
        shapes="<path d='M24,14H12V8a4,4,0,0,1,8,0h2A6,6,0,0,0,10,8v6H8a2,2,0,0,0-2,2V28a2,2,0,0,0,2,2H24a2,2,0,0,0,2-2V16A2,2,0,0,0,24,14Zm0,14H8V16H24Z'/>",
        source_url="https://raw.githubusercontent.com/carbon-design-system/carbon/main/packages/icons/src/svg/32/unlocked.svg",
        sha256="d8797e1ee9240a3ee5d0d124fee2979342c651a1c577617a964aa30cc61cd9df",
    ),
    "arrow--up": IconSpec(
        name="arrow--up",
        view_box="0 0 32 32",
        shapes="<polygon points='16 4 6 14 7.41 15.41 15 7.83 15 28 17 28 17 7.83 24.59 15.41 26 14 16 4' />",
        source_url="https://raw.githubusercontent.com/carbon-design-system/carbon/main/packages/icons/src/svg/32/arrow--up.svg",
        sha256="5ef42a6b8f7d8c23fb9ee12d8165edb486475cd2d9c5ca7b75633cc5fe1fb22b",
    ),
    "arrow--down": IconSpec(
        name="arrow--down",
        view_box="0 0 32 32",
        shapes="<polygon points='24.59 16.59 17 24.17 17 4 15 4 15 24.17 7.41 16.59 6 18 16 28 26 18 24.59 16.59' />",
        source_url="https://raw.githubusercontent.com/carbon-design-system/carbon/main/packages/icons/src/svg/32/arrow--down.svg",
        sha256="1f5817e9a2aa65dd4fefb31775e9e0cf5470f7fa51d9c90e0605505a0117ffaf",
    ),
}

ALLOWED_ICON_CLASS_TOKENS = frozenset(
    {"cds-icon", "cds-icon--sm", "cds-icon--md", "cds-icon--lg"}
)


def _validate_icon_class_name(class_name: str) -> str:
    tokens = [token for token in class_name.split() if token]
    if not tokens:
        return "cds-icon"
    if any(token not in ALLOWED_ICON_CLASS_TOKENS for token in tokens):
        raise ValueError("Unsupported icon class token")
    return " ".join(tokens)


def render_icon(
    name: str,
    *,
    class_name: str = "cds-icon",
    aria_hidden: bool = True,
    title: str = "",
) -> str:
    spec = ICONS.get(name)
    if spec is None:
        raise ValueError(f"Unsupported icon: {name}")

    safe_class = _validate_icon_class_name(class_name)
    if aria_hidden:
        return (
            f'<svg class="{safe_class}" viewBox="{spec.view_box}" '
            f'fill="currentColor" aria-hidden="true" focusable="false">'
            f"{spec.shapes}</svg>"
        )

    if not isinstance(title, str) or not title.strip():
        raise ValueError("Visible icons require a title")
    safe_title = escape(title, quote=True)
    return (
        f'<svg class="{safe_class}" viewBox="{spec.view_box}" fill="currentColor" '
        f'role="img" aria-label="{safe_title}" focusable="false">'
        f"<title>{safe_title}</title>{spec.shapes}</svg>"
    )


__all__ = [
    "IconSpec",
    "ICONS",
    "ALLOWED_ICON_CLASS_TOKENS",
    "render_icon",
]
