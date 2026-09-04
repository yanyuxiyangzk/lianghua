"""🎭 角色管理：角色的新增 / 编辑 / 菜单授权 / 启停 / 删除。

角色-菜单多对多绑定落库 /data/authsys.db（sys_role / sys_role_menu）。
"""

import pandas as pd
import streamlit as st

import authsys

st.title("🎭 角色管理")
st.caption("角色 = 菜单权限的集合；一个用户可绑定多个角色，权限取并集。")


def _menu_indent(m: dict, depth: int) -> str:
    icon = m["icon"] or ""
    return ("　" * depth) + f"{icon} {m['name']}"


def _walk(tree: list[dict], depth: int = 0) -> list[tuple[dict, int]]:
    """菜单树 → [(menu, depth)] 先序展开。"""
    out = []
    for m in sorted(tree, key=lambda x: (x["sort"], x["id"])):
        out.append((m, depth))
        out += _walk(m["children"], depth + 1)
    return out


# ---------------------------------------------------------------- 新增角色
with st.expander("➕ 新增角色"):
    with st.form("role_add", clear_on_submit=True):
        c1, c2 = st.columns(2)
        with c1:
            code = st.text_input("角色编码 *", placeholder="如 analyst / trader / viewer")
        with c2:
            name = st.text_input("角色名称 *", placeholder="如 分析师")
        remark = st.text_input("备注")
        if st.form_submit_button("创建角色", type="primary"):
            try:
                authsys.create_role(code, name, remark=remark)
                st.success(f"角色 {name} 创建成功")
                st.rerun()
            except ValueError as e:
                st.error(str(e))

# ---------------------------------------------------------------- 角色列表
roles = authsys.list_roles()
if not roles:
    st.info("暂无角色")
    st.stop()

df = pd.DataFrame([{
    "ID": r["id"], "编码": r["code"], "名称": r["name"], "备注": r["remark"] or "-",
    "状态": "✅ 启用" if r["status"] else "⛔ 停用",
    "菜单数": len(authsys.get_role_menu_ids(r["id"])),
    "创建时间": r["created_at"],
} for r in roles])
st.dataframe(df, width="stretch", hide_index=True)

# ---------------------------------------------------------------- 操作
st.subheader("🔧 角色操作", anchor=False)
rid = st.selectbox("选择角色", [r["id"] for r in roles],
                   format_func=lambda i: next((r["name"] for r in roles if r["id"] == i), str(i)))
sel = next(r for r in roles if r["id"] == rid)
tree = authsys.menu_tree()

tab_auth, tab_edit, tab_other = st.tabs(["📑 菜单授权", "✏️ 编辑", "🚫 启停/删除"])

with tab_auth:
    flat = _walk(tree)
    st.caption("勾选该角色可访问的菜单（包含父目录即包含全部子项；保存后立即生效）")

    @st.fragment
    def menu_picker():
        sel_flat = [m for m, _ in flat]
        cur_ids = set(authsys.get_role_menu_ids(rid))
        chosen_ids = []
        with st.container(border=True, height=480):
            for m, depth in flat:
                key = f"rme_{rid}_{m['id']}"
                if key not in st.session_state:
                    st.session_state[key] = m["id"] in cur_ids
                if st.checkbox(_menu_indent(m, depth), key=key,
                               disabled=m["status"] == 0):
                    pass
        for m in sel_flat:
            if st.session_state.get(f"rme_{rid}_{m['id']}", False):
                chosen_ids.append(m["id"])
        if st.button("💾 保存授权", key=f"rm_save_{rid}", type="primary"):
            authsys.set_role_menus(rid, chosen_ids)
            st.success(f"角色 {sel['name']} 菜单授权已保存（{len(chosen_ids)} 项）")
            st.rerun()

    menu_picker()

with tab_edit:
    with st.form(f"role_edit_{rid}"):
        c1, c2 = st.columns(2)
        with c1:
            new_code = st.text_input("角色编码 *", value=sel["code"])
        with c2:
            new_name = st.text_input("角色名称 *", value=sel["name"])
        new_status = 1 if st.checkbox("启用", value=bool(sel["status"])) else 0
        new_remark = st.text_input("备注", value=sel["remark"] or "")
        if st.form_submit_button("保存修改", type="primary"):
            try:
                authsys.update_role(rid, code=new_code, name=new_name,
                                    remark=new_remark, status=new_status)
                st.success("已保存")
                st.rerun()
            except ValueError as e:
                st.error(str(e))

with tab_other:
    c1, c2 = st.columns(2)
    with c1:
        lbl = "⛔ 停用" if sel["status"] else "✅ 启用"
        if st.button(lbl, key=f"role_toggle_{rid}"):
            authsys.update_role(rid, status=0 if sel["status"] else 1)
            st.rerun()
    with c2:
        if sel["code"] == "superadmin":
            st.warning("内置超级管理员角色不允许删除")
        elif st.checkbox("确认删除（同时解绑用户与菜单关联）", key=f"role_del_cfm_{rid}"):
            if st.button("🗑 删除角色", type="primary", key=f"role_del_{rid}"):
                authsys.delete_role(rid)
                st.success("已删除")
                st.rerun()
