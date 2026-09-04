"""📑 菜单管理：系统菜单树的新增 / 编辑 / 启停 / 删除。

菜单树（dir 目录 / menu 页面 / button 按钮）落库 /data/authsys.db（sys_menu），
与 app.py 导航 url_path 一一对应，供角色授权引用。
"""

import streamlit as st

import authsys

st.title("📑 菜单管理")
st.caption("菜单树 = 页面导航与权限点；url_path 与 app.py 的 st.Page url_path 对应。")

_META = {"dir": "📁 目录", "menu": "📄 页面", "button": "🔘 按钮"}


def _walk(tree: list[dict], depth: int = 0) -> list[tuple[dict, int]]:
    out = []
    for m in sorted(tree, key=lambda x: (x["sort"], x["id"])):
        out.append((m, depth))
        out += _walk(m["children"], depth + 1)
    return out


tree = authsys.menu_tree()
flat = _walk(tree)

st.subheader("🌳 菜单树", anchor=False)
rows = [{
    "ID": m["id"], "菜单": ("　" * d) + f"{m['icon'] or ''} {m['name']}",
    "类型": _META.get(m["mtype"], m["mtype"]),
    "url_path": m["url_path"] or "-", "权限码": m["perm_code"] or "-",
    "排序": m["sort"], "状态": "✅" if m["status"] else "⛔",
} for m, d in flat]
if rows:
    st.dataframe(rows, width="stretch", hide_index=True)

# ---------------------------------------------------------------- 新增菜单
with st.expander("➕ 新增菜单"):
    with st.form("menu_add", clear_on_submit=True):
        c1, c2 = st.columns(2)
        with c1:
            parent_id = st.selectbox("父菜单", [0] + [m["id"] for m, _ in flat],
                                     format_func=lambda i: "（根目录）" if i == 0 else
                                     next((_META.get(m["mtype"], "") + " " + m["name"]
                                           for m, _ in flat if m["id"] == i), str(i)))
            mtype = st.selectbox("类型", ["dir", "menu", "button"],
                                 format_func=lambda t: _META[t])
        with c2:
            name = st.text_input("名称 *", placeholder="如 用户管理")
            icon = st.text_input("图标（emoji，可空）", placeholder="如 👥")
        c3, c4 = st.columns(2)
        with c3:
            url_path = st.text_input("url_path（页面路由，目录/按钮可空）",
                                     placeholder="如 admin-users")
        with c4:
            perm_code = st.text_input("权限码（按钮级鉴权用，可空）",
                                      placeholder="如 btn:user:delete")
        c5, c6 = st.columns(2)
        with c5:
            sort = st.number_input("排序", min_value=0, value=0, step=1)
        with c6:
            status = 1 if st.checkbox("启用", value=True) else 0
        if st.form_submit_button("创建菜单", type="primary"):
            try:
                authsys.create_menu(parent_id, name, mtype=mtype, icon=icon,
                                    url_path=url_path, perm_code=perm_code,
                                    sort=sort, status=status)
                st.success(f"菜单 {name} 创建成功")
                st.rerun()
            except ValueError as e:
                st.error(str(e))

# ---------------------------------------------------------------- 编辑/删除
st.subheader("🔧 菜单操作", anchor=False)
mid = st.selectbox("选择菜单", [m["id"] for m, _ in flat],
                   format_func=lambda i: next((("　" * d) + f"{m['icon'] or ''} {m['name']}"
                                               for m, d in flat if m["id"] == i), str(i)))
sel = next(m for m, _ in flat if m["id"] == mid)

tab_edit, tab_other = st.tabs(["✏️ 编辑", "🚫 启停/删除"])

with tab_edit:
    with st.form(f"menu_edit_{mid}"):
        c1, c2 = st.columns(2)
        with c1:
            new_parent_opts = [0] + [m["id"] for m, _ in flat if m["id"] != mid]
            new_parent = st.selectbox("父菜单", new_parent_opts,
                                      index=new_parent_opts.index(sel["parent_id"])
                                      if sel["parent_id"] in new_parent_opts else 0,
                                      format_func=lambda i: "（根目录）" if i == 0 else
                                      next((("　" * d) + m["name"] for m, d in flat if m["id"] == i), str(i)))
            new_type = st.selectbox("类型", ["dir", "menu", "button"],
                                    index=["dir", "menu", "button"].index(sel["mtype"]),
                                    format_func=lambda t: _META[t])
        with c2:
            new_name = st.text_input("名称 *", value=sel["name"])
            new_icon = st.text_input("图标", value=sel["icon"] or "")
        c3, c4 = st.columns(2)
        with c3:
            new_url = st.text_input("url_path", value=sel["url_path"] or "")
        with c4:
            new_perm = st.text_input("权限码", value=sel["perm_code"] or "")
        c5, c6 = st.columns(2)
        with c5:
            new_sort = st.number_input("排序", min_value=0, value=int(sel["sort"] or 0), step=1)
        with c6:
            new_status = 1 if st.checkbox("启用", value=bool(sel["status"])) else 0
        if st.form_submit_button("保存修改", type="primary"):
            try:
                authsys.update_menu(mid, parent_id=new_parent, name=new_name,
                                    mtype=new_type, icon=new_icon, url_path=new_url or None,
                                    perm_code=new_perm or None, sort=new_sort, status=new_status)
                st.success("已保存")
                st.rerun()
            except ValueError as e:
                st.error(str(e))

with tab_other:
    c1, c2 = st.columns(2)
    with c1:
        lbl = "⛔ 停用" if sel["status"] else "✅ 启用"
        if st.button(lbl, key=f"menu_toggle_{mid}"):
            authsys.update_menu(mid, status=0 if sel["status"] else 1)
            st.rerun()
    with c2:
        if sel["children"]:
            st.warning("存在子菜单，请先删除子菜单")
        elif st.checkbox("确认删除（同时移除各角色的该项授权）", key=f"menu_del_cfm_{mid}"):
            if st.button("🗑 删除菜单", type="primary", key=f"menu_del_{mid}"):
                try:
                    authsys.delete_menu(mid)
                    st.success("已删除")
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))
