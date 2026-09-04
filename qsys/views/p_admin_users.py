"""👥 用户管理：系统用户的新增 / 编辑 / 改密 / 角色分配 / 启停 / 删除。

数据落库 /data/authsys.db（sys_user / sys_user_role）。
默认账号 admin / admin123（超级管理员，请尽快修改密码）。
"""

import pandas as pd
import streamlit as st

import authsys

st.title("👥 用户管理")
st.caption("系统账号与角色绑定管理。密码 PBKDF2 加盐哈希存储，不落明文。")

# ---------------------------------------------------------------- 总览
s = authsys.stats()
c1, c2, c3, c4 = st.columns(4)
c1.metric("用户数", s["users"])
c2.metric("角色数", s["roles"])
c3.metric("菜单数", s["menus"])
c4.metric("组织数", s["orgs"])

# ---------------------------------------------------------------- 新增用户
with st.expander("➕ 新增用户"):
    with st.form("user_add", clear_on_submit=True):
        f1, f2 = st.columns(2)
        with f1:
            username = st.text_input("登录名 *", placeholder="英文/数字，如 zhangsan")
            password = st.text_input("初始密码 *", type="password", placeholder="至少 6 位")
        with f2:
            name = st.text_input("姓名", placeholder="如 张三")
            org_id = st.selectbox("所属组织",
                                  options=[0] + [o["id"] for o in authsys.list_orgs()],
                                  format_func=lambda i: "（无）" if i == 0 else
                                  next((o["name"] for o in authsys.list_orgs() if o["id"] == i), str(i)))
        f3, f4 = st.columns(2)
        with f3:
            email = st.text_input("邮箱")
        with f4:
            phone = st.text_input("手机号")
        f5, f6 = st.columns(2)
        with f5:
            status = 1 if st.checkbox("启用账号", value=True) else 0
        with f6:
            is_superadmin = 1 if st.checkbox("超级管理员（跳过一切权限校验）") else 0
        remark = st.text_input("备注")
        if st.form_submit_button("创建用户", type="primary"):
            try:
                authsys.create_user(username, password, name=name, org_id=org_id,
                                    email=email, phone=phone, status=status,
                                    is_superadmin=is_superadmin, remark=remark)
                st.success(f"用户 {username} 创建成功")
                st.rerun()
            except ValueError as e:
                st.error(str(e))

# ---------------------------------------------------------------- 列表
kw = st.text_input("🔍 搜索（用户名/姓名）", key="user_search")
users = authsys.list_users(kw)
org_map = {o["id"]: o["name"] for o in authsys.list_orgs()}

if not users:
    st.info("暂无用户")
else:
    df = pd.DataFrame([{
        "ID": u["id"], "登录名": u["username"], "姓名": u["name"] or "-",
        "组织": org_map.get(u["org_id"], "-") if u["org_id"] else "-",
        "状态": "✅ 启用" if u["status"] else "⛔ 停用",
        "超管": "⭐" if u["is_superadmin"] else "",
        "邮箱": u["email"] or "-", "手机": u["phone"] or "-",
        "最近登录": u["last_login"] or "-", "创建时间": u["created_at"] or "-",
    } for u in users])
    st.dataframe(df, width="stretch", hide_index=True)

# ---------------------------------------------------------------- 操作
st.subheader("🔧 用户操作", anchor=False)
uid = st.selectbox("选择用户", [u["id"] for u in users],
                   format_func=lambda i: next((f"{u['username']}（{u['name'] or '未填姓名'}）"
                                               for u in users if u["id"] == i), str(i)))
sel = next(u for u in users if u["id"] == uid)
roles = authsys.list_roles()

tab_edit, tab_pwd, tab_role, tab_other = st.tabs(["✏️ 编辑资料", "🔑 重置密码", "🎭 分配角色", "🚫 启停/删除"])

with tab_edit:
    with st.form(f"user_edit_{uid}"):
        e1, e2 = st.columns(2)
        with e1:
            new_username = st.text_input("登录名 *", value=sel["username"])
            new_email = st.text_input("邮箱", value=sel["email"] or "")
        with e2:
            new_name = st.text_input("姓名", value=sel["name"] or "")
            new_phone = st.text_input("手机号", value=sel["phone"] or "")
        new_org = st.selectbox("所属组织",
                               options=[0] + list(org_map.keys()),
                               index=([0] + list(org_map.keys())).index(sel["org_id"]),
                               format_func=lambda i: "（无）" if i == 0 else org_map[i])
        c1, c2 = st.columns(2)
        with c1:
            new_status = 1 if st.checkbox("启用账号", value=bool(sel["status"])) else 0
        with c2:
            new_super = 1 if st.checkbox("超级管理员", value=bool(sel["is_superadmin"])) else 0
        new_remark = st.text_input("备注", value=sel["remark"] or "")
        if st.form_submit_button("保存修改", type="primary"):
            try:
                authsys.update_user(uid, username=new_username, name=new_name,
                                    org_id=new_org, email=new_email, phone=new_phone,
                                    status=new_status, is_superadmin=new_super,
                                    remark=new_remark)
                st.success("已保存")
                st.rerun()
            except ValueError as e:
                st.error(str(e))

with tab_pwd:
    with st.form(f"user_pwd_{uid}"):
        npwd = st.text_input("新密码 *", type="password", placeholder="至少 6 位")
        if st.form_submit_button("重置密码"):
            try:
                authsys.reset_password(uid, npwd)
                st.success(f"用户 {sel['username']} 密码已重置")
            except ValueError as e:
                st.error(str(e))

with tab_role:
    cur_role_ids = {r["id"] for r in authsys.get_user_roles(uid)}
    chosen = st.multiselect("角色（可多选）", [r["id"] for r in roles],
                            default=list(cur_role_ids),
                            format_func=lambda i: next((r["name"] for r in roles if r["id"] == i), str(i)))
    if st.button("保存角色", key=f"user_role_save_{uid}"):
        authsys.set_user_roles(uid, chosen)
        st.success("角色已更新")
        st.rerun()

with tab_other:
    c1, c2 = st.columns(2)
    with c1:
        lbl = "⛔ 停用" if sel["status"] else "✅ 启用"
        if st.button(lbl, key=f"user_toggle_{uid}"):
            authsys.update_user(uid, status=0 if sel["status"] else 1)
            st.rerun()
    with c2:
        if sel["is_superadmin"]:
            st.warning("超级管理员账号不允许删除，可先取消超管标记")
        else:
            if st.checkbox("确认删除该用户（同时解绑其所有角色）", key=f"user_del_cfm_{uid}"):
                if st.button("🗑 删除用户", type="primary", key=f"user_del_{uid}"):
                    authsys.delete_user(uid)
                    st.success("已删除")
                    st.rerun()
