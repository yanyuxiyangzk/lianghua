"""全局主题：浅色（Streamlit 默认白）/ 深色（量化驾驶舱同款）。

机制（Streamlit 1.61 原生双主题 + 定制 CSS）：
  - .streamlit/config.toml 定义 [theme.dark]（驾驶舱配色）；前端原生支持
    Light/Dark 运行时切换，选择持久化在浏览器 localStorage
    （key = stActiveTheme-<pathname>-v2，值 = JSON 的 "Light"/"Dark"）
  - 本模块把选择持久化到 /data/theme.json；apply() 注入 JS 同步
    localStorage 并在不一致时 reload——原生换肤（dataframe 等全部跟随）
  - 深色下再补一层定制 CSS：侧栏质感、卡片渐变、驾驶舱细节
"""

import json

import streamlit as st

from common import DATA_DIR

THEME_FILE = DATA_DIR / "theme.json"
DEFAULT = "light"

# ---------------------------------------------------------------- 深色补充样式
# 原生深色主题已覆盖内建组件，这里只做侧栏质感、卡片渐变等驾驶舱细节
_DARK_EXTRA_CSS = """
<style>
.stApp { background: linear-gradient(160deg, #081018 0%, #101b28 100%); }
[data-testid="stHeader"] { background: rgba(8,16,24,.6); }
[data-testid="stSidebar"] { background: #0b141e; border-right: 1px solid #16232f; }
[data-testid="stSidebar"] * { color: #9aaabd; }
[data-testid="stVerticalBlockBorderWrapper"] {
    background: linear-gradient(135deg, #172332 0%, #101923 100%) !important;
    border: 1px solid #223243 !important; border-radius: 14px !important;
}
div[data-baseweb="select"] > div, .stTextInput input {
    background: #101923 !important; color: #dbe5ef !important;
    border-color: #223243 !important;
}
.stButton button, .stFormSubmitButton button {
    background: #101923; color: #dbe5ef; border: 1px solid #223243;
}
.stButton button[kind="primary"], .stFormSubmitButton button[kind="primary"] {
    background: #216fe9; border-color: #216fe9; color: #fff;
}
.stTabs [data-baseweb="tab-list"] { gap: 4px; }
.stTabs [data-baseweb="tab"] {
    background: #101923; color: #708195; border-radius: 8px 8px 0 0;
    border: 1px solid #223243;
}
.stTabs [aria-selected="true"] { background: #172332; color: #f3f7fb !important; }
hr { border-color: #223243; }
[data-testid="stCaptionContainer"] p { color: #65778a; }
</style>
"""

# 隐藏前端汉堡菜单里的原生主题选项，避免与本应用侧栏开关双轨打架
_HIDE_NATIVE_MENU_CSS = """
<style>
[data-testid^="stMainMenuItem-theme-"] { display: none !important; }
</style>
"""


def get_theme() -> str:
    """当前主题：'light' / 'dark'（文件缺失或非法回落 light）。"""
    try:
        t = json.loads(THEME_FILE.read_text()).get("theme", DEFAULT)
        return t if t in ("light", "dark") else DEFAULT
    except Exception:
        return DEFAULT


def set_theme(theme: str) -> None:
    """保存主题选择（light / dark），下次重跑 apply() 后生效。"""
    if theme not in ("light", "dark"):
        raise ValueError(f"非法主题: {theme}")
    THEME_FILE.parent.mkdir(parents=True, exist_ok=True)
    THEME_FILE.write_text(json.dumps({"theme": theme}, ensure_ascii=False))


def apply() -> None:
    """全局应用当前主题。app.py 每次重跑调用，对每个页面生效。

    JS 部分：把 theme.json 的选择同步到前端 localStorage（原生主题切换的
    持久化位），并在原生主题与目标不一致时 reload 页面——浏览器加载时会用
    localStorage 里的主题重建整个 UI（dataframe/输入框等全部原生换肤）。
    """
    t = get_theme()
    st.markdown(_HIDE_NATIVE_MENU_CSS, unsafe_allow_html=True)
    if t == "dark":
        st.markdown(_DARK_EXTRA_CSS, unsafe_allow_html=True)
    want = "Dark" if t == "dark" else "Light"
    st.components.v1.html(
        f"""<script>
        const pdoc = window.parent.document;
        const key = 'stActiveTheme-' + window.parent.location.pathname + '-v2';
        let stored = null;
        try {{ stored = JSON.parse(window.parent.localStorage.getItem(key)); }} catch(e) {{}}
        const nativeDark = stored === 'Dark';      // null → 跟随系统(base=light)
        if (stored !== '{want}') {{
            window.parent.localStorage.setItem(key, JSON.stringify('{want}'));
        }}
        if (nativeDark !== ('{want}' === 'Dark')) {{
            window.parent.location.reload();
        }}
        </script>""",
        height=0)
