"""🏢 组织管理：组织（部门）树的新增 / 编辑 / 启停 / 删除。

组织树落库 /data/authsys.db（sys_org），用户通过 org_id 归属组织。
"""

import streamlit as st

import authsys

st.title("🏢 组织管理")
st.caption("组织 = 部门/团队树；用户在「用户管理」中选择所属组织。")


def _walk(tree: list[dict], depth: int = 0) -> list[tuple[dict, int]]:
    out = []
    for o in sorted(tree, key=lambda x: (x["sort"], x["id"])):
        out.append((o, depth))
        out += _walk(o["children"], depth + 1)
    return out


def _user_count(org_id: int) -> int:
    return sum(1 for u in authsys.list_users() if u["org_id"] == org_id)


tree = authsys.org_tree()
flat = _walk(tree)

st.subheader("🌳 组织架构", anchor=False)
rows = [{
    "ID": o["id"], "组织": ("　" * d) + o["name"],
    "编码": o["code"] or "-", "排序": o["sort"],
    "用户数": _user_count(o["id"]),
    "状态": "✅" if o["status"] else "⛔", "备注": o["remark"] or "-",
} for o, d in flat]
if rows:
    st.dataframe(rows, width="stretch", hide_index=True)

# ---------------------------------------------------------------- 新增组织
with st.expander("➕ 新增组织"):
    with st.form("org_add", clear_on_submit=True):
        c1, c2 = st.columns(2)
        with c1:
            parent_id = st.selectbox("上级组织", [0] + [o["id"] for o, _ in flat],
                                     format_func=lambda i: "（根组织）" if i == 0 else
                                     next((("　" * d) + o["name"] for o, d in flat
                                           if o["id"] == i), str(i)))
            code = st.text_input("编码", placeholder="如 QUANT / IT")
        with c2:
            name = st.text_input("名称 *", placeholder="如 量化研究部")
            sort = st.number_input("排序", min_value=0, value=0, step=1)
        c3, c4 = st.columns(2)
        with c3:
            status = 1 if st.checkbox("启用", value=True) else 0
        with c4:
            remark = st.text_input("备注")
        if st.form_submit_button("创建组织", type="primary"):
            try:
                authsys.create_org(parent_id, name, code=code, sort=sort,
                                   status=status, remark=remark)
                st.success(f"组织 {name} 创建成功")
                st.rerun()
            except ValueError as e:
                st.error(str(e))

# ---------------------------------------------------------------- 编辑/删除
st.subheader("🔧 组织操作", anchor=False)
oid = st.selectbox("选择组织", [o["id"] for o, _ in flat],
                   format_func=lambda i: next((("　" * d) + o["name"]
                                               for o, d in flat if o["id"] == i), str(i)))
sel = next(o for o, _ in flat if o["id"] == oid)

tab_edit, tab_other = st.tabs(["✏️ 编辑", "🚫 启停/删除"])

with tab_edit:
    with st.form(f"org_edit_{oid}"):
        c1, c2 = st.columns(2)
        with c1:
            new_parent_opts = [0] + [o["id"] for o, _ in flat if o["id"] != oid]
            new_parent = st.selectbox("上级组织", new_parent_opts,
                                      index=new_parent_opts.index(sel["parent_id"])
                                      if sel["parent_id"] in new_parent_opts else 0,
                                      format_func=lambda i: "（根组织）" if i == 0 else
                                      next((("　" * d) + o["name"] for o, d in flat
                                            if o["id"] == i), str(i)))
            new_code = st.text_input("编码", value=sel["code"] or "")
        with c2:
            new_name = st.text_input("名称 *", value=sel["name"])
            new_sort = st.number_input("排序", min_value=0, value=int(sel["sort"] or 0), step=1)
        c3, c4 = st.columns(2)
        with c3:
            new_status = 1 if st.checkbox("启用", value=bool(sel["status"])) else 0
        with c4:
            new_remark = st.text_input("备注", value=sel["remark"] or "")
        if st.form_submit_button("保存修改", type="primary"):
            try:
                authsys.update_org(oid, parent_id=new_parent, name=new_name,
                                   code=new_code, sort=new_sort, status=new_status,
                                   remark=new_remark)
                st.success("已保存")
                st.rerun()
            except ValueError as e:
                st.error(str(e))

with tab_other:
    c1, c2 = st.columns(2)
    with c1:
        lbl = "⛔ 停用" if sel["status"] else "✅ 启用"
        if st.button(lbl, key=f"org_toggle_{oid}"):
            authsys.update_org(oid, status=0 if sel["status"] else 1)
            st.rerun()
    with c2:
        if sel["children"]:
            st.warning("存在子组织，请先删除子组织")
        elif _user_count(oid) > 0:
            st.warning("该组织下仍有用户，请先调整用户归属")
        elif st.checkbox("确认删除", key=f"org_del_cfm_{oid}"):
            if st.button("🗑 删除组织", type="primary", key=f"org_del_{oid}"):
                authsys.delete_org(oid)
                st.success("已删除")
                st.rerun()
