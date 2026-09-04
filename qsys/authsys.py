"""RBAC 权限系统：用户 / 角色 / 菜单 / 组织（SQLite，/data/authsys.db）。

表结构：
  sys_org        组织表（部门树，parent_id 自关联）
  sys_user       用户表（密码 PBKDF2-SHA256 加盐哈希，不含明文）
  sys_role       角色表（角色-用户、角色-菜单 多对多）
  sys_menu       菜单表（树形：dir 目录 / menu 页面 / button 按钮，url_path 对齐 app.py）
  sys_user_role  用户-角色 关联表
  sys_role_menu  角色-菜单 关联表

权限模型：
  - 用户可选归属一个组织；可绑定多个角色；角色绑定多个菜单
  - is_superadmin=1 的用户跳过一切权限校验
  - authenticate() 校验用户名密码；get_user_perms() 返回用户可见的
    url_path 与 perm_code 集合，供页面级/按钮级鉴权
  - 首次建库自动播种：默认组织「量化科技」、超级管理员角色、
    admin 用户（admin / admin123，首次登录后建议立即改密）、
    与 app.py 导航一致的菜单树
"""

import hashlib
import hmac
import secrets
import sqlite3
from datetime import datetime
from pathlib import Path

from common import DATA_DIR

DB_PATH = DATA_DIR / "authsys.db"

# ---------------------------------------------------------------- 建表
_SCHEMA = """
CREATE TABLE IF NOT EXISTS sys_org (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id  INTEGER NOT NULL DEFAULT 0,
    name       TEXT NOT NULL,
    code       TEXT,
    sort       INTEGER DEFAULT 0,
    status     INTEGER DEFAULT 1,          -- 1 启用 / 0 停用
    remark     TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS sys_user (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    salt          TEXT NOT NULL,
    name          TEXT,
    org_id        INTEGER DEFAULT 0,
    email         TEXT,
    phone         TEXT,
    status        INTEGER DEFAULT 1,       -- 1 启用 / 0 停用
    is_superadmin INTEGER DEFAULT 0,
    remark        TEXT,
    last_login    TEXT,
    created_at    TEXT,
    updated_at    TEXT
);
CREATE TABLE IF NOT EXISTS sys_role (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    code       TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL,
    remark     TEXT,
    status     INTEGER DEFAULT 1,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS sys_menu (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id INTEGER NOT NULL DEFAULT 0,
    name      TEXT NOT NULL,
    icon      TEXT,
    url_path  TEXT,
    mtype     TEXT DEFAULT 'menu',         -- dir / menu / button
    perm_code TEXT,
    sort      INTEGER DEFAULT 0,
    status    INTEGER DEFAULT 1,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS sys_user_role (
    user_id INTEGER NOT NULL,
    role_id INTEGER NOT NULL,
    UNIQUE(user_id, role_id)
);
CREATE TABLE IF NOT EXISTS sys_role_menu (
    role_id INTEGER NOT NULL,
    menu_id INTEGER NOT NULL,
    UNIQUE(role_id, menu_id)
);
"""


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    _seed(c)
    _ensure_menu_sync(c)
    return c


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 密码
_PBKDF2_ITER = 100_000


def hash_password(pwd: str, salt: str | None = None) -> tuple[str, str]:
    """返回 (password_hash, salt)。salt 缺省时随机生成。"""
    if not pwd:
        raise ValueError("密码不能为空")
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pwd.encode(), salt.encode(), _PBKDF2_ITER)
    return dk.hex(), salt


def verify_password(pwd: str, salt: str, password_hash: str) -> bool:
    dk = hashlib.pbkdf2_hmac("sha256", pwd.encode(), salt.encode(), _PBKDF2_ITER)
    return hmac.compare_digest(dk.hex(), password_hash)


# ---------------------------------------------------------------- 用户
def list_users(keyword: str = "") -> list[dict]:
    sql = ("SELECT u.*, o.name AS org_name FROM sys_user u "
           "LEFT JOIN sys_org o ON u.org_id = o.id")
    with _conn() as c:
        if keyword.strip():
            sql += " WHERE u.username LIKE ? OR u.name LIKE ?"
            rows = c.execute(sql + " ORDER BY u.id",
                             (f"%{keyword}%", f"%{keyword}%")).fetchall()
        else:
            rows = c.execute(sql + " ORDER BY u.id").fetchall()
    return [dict(r) for r in rows]


def get_user(user_id: int) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM sys_user WHERE id=?", (user_id,)).fetchone()
    return dict(r) if r else None


def get_user_by_name(username: str) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM sys_user WHERE username=?", (username,)).fetchone()
    return dict(r) if r else None


def create_user(username: str, password: str, name: str = "", org_id: int = 0,
                email: str = "", phone: str = "", status: int = 1,
                is_superadmin: int = 0, remark: str = "") -> int:
    username = username.strip()
    if not username or not password:
        raise ValueError("用户名和密码不能为空")
    pwd_hash, salt = hash_password(password)
    with _conn() as c:
        try:
            cur = c.execute(
                """INSERT INTO sys_user (username, password_hash, salt, name, org_id,
                       email, phone, status, is_superadmin, remark, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (username, pwd_hash, salt, name, org_id, email, phone,
                 status, is_superadmin, remark, _now(), _now()))
        except sqlite3.IntegrityError:
            raise ValueError(f"用户名 {username} 已存在")
    return cur.lastrowid


def update_user(user_id: int, **fields) -> None:
    allowed = {"username", "name", "org_id", "email", "phone", "status",
               "is_superadmin", "remark"}
    sets, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "username" and not v.strip():
            raise ValueError("用户名不能为空")
        sets.append(f"{k}=?")
        vals.append(v.strip() if isinstance(v, str) else v)
    if not sets:
        return
    sets.append("updated_at=?")
    vals.append(_now())
    vals.append(user_id)
    with _conn() as c:
        try:
            c.execute(f"UPDATE sys_user SET {', '.join(sets)} WHERE id=?", vals)
        except sqlite3.IntegrityError:
            raise ValueError("用户名已存在")


def reset_password(user_id: int, new_pwd: str) -> None:
    pwd_hash, salt = hash_password(new_pwd)
    with _conn() as c:
        c.execute("UPDATE sys_user SET password_hash=?, salt=?, updated_at=? WHERE id=?",
                  (pwd_hash, salt, _now(), user_id))


def delete_user(user_id: int) -> None:
    with _conn() as c:
        c.execute("DELETE FROM sys_user_role WHERE user_id=?", (user_id,))
        c.execute("DELETE FROM sys_user WHERE id=?", (user_id,))


def touch_login(user_id: int) -> None:
    with _conn() as c:
        c.execute("UPDATE sys_user SET last_login=? WHERE id=?", (_now(), user_id))


def set_user_roles(user_id: int, role_ids: list[int]) -> None:
    with _conn() as c:
        c.execute("DELETE FROM sys_user_role WHERE user_id=?", (user_id,))
        c.executemany("INSERT INTO sys_user_role (user_id, role_id) VALUES (?,?)",
                      [(user_id, r) for r in role_ids])


def get_user_roles(user_id: int) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT r.* FROM sys_role r JOIN sys_user_role ur ON r.id=ur.role_id "
            "WHERE ur.user_id=?", (user_id,)).fetchall()
    return [dict(r) for r in rows]


def authenticate(username: str, password: str) -> dict | None:
    """校验用户名密码。成功返回用户 dict（并记录登录时间），失败返回 None。"""
    u = get_user_by_name(username.strip())
    if not u or u["status"] != 1:
        return None
    if not verify_password(password, u["salt"], u["password_hash"]):
        return None
    touch_login(u["id"])
    u["last_login"] = _now()
    return u


def get_user_perms(user_id: int) -> dict[str, set]:
    """返回 {'urls': set, 'perms': set}——用户所有角色绑定的菜单权限（超管全量）。"""
    u = get_user(user_id)
    if not u:
        return {"urls": set(), "perms": set()}
    if u["is_superadmin"]:
        with _conn() as c:
            rows = c.execute("SELECT url_path, perm_code FROM sys_menu WHERE status=1").fetchall()
    else:
        with _conn() as c:
            rows = c.execute(
                """SELECT DISTINCT m.url_path, m.perm_code FROM sys_menu m
                   JOIN sys_role_menu rm ON m.id=rm.menu_id
                   JOIN sys_user_role ur ON ur.role_id=rm.role_id
                   WHERE ur.user_id=? AND m.status=1""", (user_id,)).fetchall()
    urls = {r["url_path"] for r in rows if r["url_path"]}
    perms = {r["perm_code"] for r in rows if r["perm_code"]}
    return {"urls": urls, "perms": perms}


# ---------------------------------------------------------------- 角色
def list_roles() -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM sys_role ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def create_role(code: str, name: str, remark: str = "", status: int = 1) -> int:
    code, name = code.strip(), name.strip()
    if not code or not name:
        raise ValueError("角色编码和名称不能为空")
    with _conn() as c:
        try:
            cur = c.execute("INSERT INTO sys_role (code, name, remark, status, created_at) "
                            "VALUES (?,?,?,?,?)", (code, name, remark, status, _now()))
        except sqlite3.IntegrityError:
            raise ValueError(f"角色编码 {code} 已存在")
    return cur.lastrowid


def update_role(role_id: int, **fields) -> None:
    allowed = {"code", "name", "remark", "status"}
    sets, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        sets.append(f"{k}=?")
        vals.append(v.strip() if isinstance(v, str) else v)
    if not sets:
        return
    vals.append(role_id)
    with _conn() as c:
        try:
            c.execute(f"UPDATE sys_role SET {', '.join(sets)} WHERE id=?", vals)
        except sqlite3.IntegrityError:
            raise ValueError("角色编码已存在")


def delete_role(role_id: int) -> None:
    with _conn() as c:
        c.execute("DELETE FROM sys_user_role WHERE role_id=?", (role_id,))
        c.execute("DELETE FROM sys_role_menu WHERE role_id=?", (role_id,))
        c.execute("DELETE FROM sys_role WHERE id=?", (role_id,))


def set_role_menus(role_id: int, menu_ids: list[int]) -> None:
    with _conn() as c:
        c.execute("DELETE FROM sys_role_menu WHERE role_id=?", (role_id,))
        c.executemany("INSERT INTO sys_role_menu (role_id, menu_id) VALUES (?,?)",
                      [(role_id, m) for m in menu_ids])


def get_role_menu_ids(role_id: int) -> list[int]:
    with _conn() as c:
        rows = c.execute("SELECT menu_id FROM sys_role_menu WHERE role_id=?",
                         (role_id,)).fetchall()
    return [r["menu_id"] for r in rows]


# ---------------------------------------------------------------- 菜单
def list_menus() -> list[dict]:
    """平铺返回全部菜单（按 parent_id, sort, id 排序），树形组装交给页面。"""
    with _conn() as c:
        rows = c.execute("SELECT * FROM sys_menu ORDER BY parent_id, sort, id").fetchall()
    return [dict(r) for r in rows]


def menu_tree() -> list[dict]:
    """返回菜单树（dir 与 menu 全部节点，含 children）。"""
    menus = list_menus()
    by_id = {m["id"]: {**m, "children": []} for m in menus}
    roots = []
    for m in by_id.values():
        if m["parent_id"] and m["parent_id"] in by_id:
            by_id[m["parent_id"]]["children"].append(m)
        else:
            roots.append(m)
    return roots


def create_menu(parent_id: int, name: str, mtype: str = "menu", icon: str = "",
                url_path: str = "", perm_code: str = "", sort: int = 0,
                status: int = 1) -> int:
    name = name.strip()
    if not name:
        raise ValueError("菜单名称不能为空")
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO sys_menu (parent_id, name, icon, url_path, mtype, perm_code, sort, status, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (parent_id, name, icon, url_path or None, mtype, perm_code or None,
             sort, status, _now()))
    return cur.lastrowid


def update_menu(menu_id: int, **fields) -> None:
    allowed = {"parent_id", "name", "icon", "url_path", "mtype", "perm_code", "sort", "status"}
    sets, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "name" and not str(v).strip():
            raise ValueError("菜单名称不能为空")
        sets.append(f"{k}=?")
        vals.append(v)
    if not sets:
        return
    vals.append(menu_id)
    with _conn() as c:
        c.execute(f"UPDATE sys_menu SET {', '.join(sets)} WHERE id=?", vals)


def delete_menu(menu_id: int) -> None:
    with _conn() as c:
        if c.execute("SELECT COUNT(*) FROM sys_menu WHERE parent_id=?", (menu_id,)).fetchone()[0]:
            raise ValueError("存在子菜单，请先删除子菜单")
        c.execute("DELETE FROM sys_role_menu WHERE menu_id=?", (menu_id,))
        c.execute("DELETE FROM sys_menu WHERE id=?", (menu_id,))


# ---------------------------------------------------------------- 组织
def list_orgs() -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM sys_org ORDER BY parent_id, sort, id").fetchall()
    return [dict(r) for r in rows]


def org_tree() -> list[dict]:
    orgs = list_orgs()
    by_id = {o["id"]: {**o, "children": []} for o in orgs}
    roots = []
    for o in by_id.values():
        if o["parent_id"] and o["parent_id"] in by_id:
            by_id[o["parent_id"]]["children"].append(o)
        else:
            roots.append(o)
    return roots


def create_org(parent_id: int, name: str, code: str = "", sort: int = 0,
               status: int = 1, remark: str = "") -> int:
    name = name.strip()
    if not name:
        raise ValueError("组织名称不能为空")
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO sys_org (parent_id, name, code, sort, status, remark, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (parent_id, name, code or None, sort, status, remark, _now()))
    return cur.lastrowid


def update_org(org_id: int, **fields) -> None:
    allowed = {"parent_id", "name", "code", "sort", "status", "remark"}
    sets, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "name" and not str(v).strip():
            raise ValueError("组织名称不能为空")
        sets.append(f"{k}=?")
        vals.append(v)
    if not sets:
        return
    vals.append(org_id)
    with _conn() as c:
        c.execute(f"UPDATE sys_org SET {', '.join(sets)} WHERE id=?", vals)


def delete_org(org_id: int) -> None:
    with _conn() as c:
        if c.execute("SELECT COUNT(*) FROM sys_org WHERE parent_id=?", (org_id,)).fetchone()[0]:
            raise ValueError("存在子组织，请先删除子组织")
        if c.execute("SELECT COUNT(*) FROM sys_user WHERE org_id=?", (org_id,)).fetchone()[0]:
            raise ValueError("该组织下仍有用户，请先调整用户归属")
        c.execute("DELETE FROM sys_org WHERE id=?", (org_id,))


# ---------------------------------------------------------------- 统计
def stats() -> dict:
    with _conn() as c:
        return {
            "users": c.execute("SELECT COUNT(*) FROM sys_user").fetchone()[0],
            "roles": c.execute("SELECT COUNT(*) FROM sys_role").fetchone()[0],
            "menus": c.execute("SELECT COUNT(*) FROM sys_menu").fetchone()[0],
            "orgs": c.execute("SELECT COUNT(*) FROM sys_org").fetchone()[0],
        }


# ---------------------------------------------------------------- 播种
def _seed(c: sqlite3.Connection) -> None:
    """首次建库：默认组织 / 超管角色 / admin 用户 / 与 app.py 一致的菜单树。"""
    if c.execute("SELECT COUNT(*) FROM sys_user").fetchone()[0] > 0:
        return

    # 组织
    c.execute("INSERT INTO sys_org (parent_id, name, code, sort, remark, created_at) "
              "VALUES (0, '量化科技', 'ROOT', 0, '默认根组织', ?)", (_now(),))
    root_org = c.execute("SELECT id FROM sys_org WHERE code='ROOT'").fetchone()[0]

    # 角色
    c.execute("INSERT INTO sys_role (code, name, remark, status, created_at) "
              "VALUES ('superadmin', '超级管理员', '拥有全部菜单权限', 1, ?)", (_now(),))
    admin_role = c.execute("SELECT id FROM sys_role WHERE code='superadmin'").fetchone()[0]

    # 管理员用户（首次登录后请立即改密）
    pwd_hash, salt = hash_password("admin123")
    c.execute(
        """INSERT INTO sys_user (username, password_hash, salt, name, org_id, status,
               is_superadmin, remark, created_at, updated_at)
           VALUES ('admin', ?, ?, '管理员', ?, 1, 1, '初始账号，请尽快修改密码', ?, ?)""",
        (pwd_hash, salt, root_org, _now(), _now()))
    admin_user = c.execute("SELECT id FROM sys_user WHERE username='admin'").fetchone()[0]
    c.execute("INSERT INTO sys_user_role (user_id, role_id) VALUES (?,?)",
              (admin_user, admin_role))

    # 菜单树：与 app.py 导航一致（清单见模块级 _MENU_SEED，插入走 _insert_menu_seed）
    _insert_menu_seed(c)
    menu_ids = [r[0] for r in c.execute("SELECT id FROM sys_menu").fetchall()]
    c.executemany("INSERT INTO sys_role_menu (role_id, menu_id) VALUES (?,?)",
                  [(admin_role, m) for m in menu_ids])


# ---------------------------------------------------------------- 菜单种子清单
# (parent, name, icon, mtype, url_path, perm_code, sort)——根节点 parent=None
_MENU_SEED = [
    (None, "我的", "🎯", "dir", None, None, 1),
    ("我的", "量化驾驶舱", "🚀", "menu", "dash", "page:dash", 0),
        ("我的", "今日执行", "🎯", "menu", "today", "page:today", 1),
        ("我的", "资金账号", "💹", "menu", "broker", "page:broker", 2),
        (None, "市场数据", "📈", "dir", None, None, 2),
        ("市场数据", "股票行情", "📈", "menu", "quotes", "page:quotes", 1),
        ("市场数据", "股票/指数列表", "📋", "menu", "stocklist", "page:stocklist", 2),
        ("市场数据", "板块行情", "🏛️", "menu", "sector", "page:sector", 3),
        ("市场数据", "自选K线", "🕯️", "menu", "kline", "page:kline", 4),
        ("市场数据", "专业K线", "📉", "menu", "kpro", "page:kpro", 5),
        ("市场数据", "板块资金流", "🌐", "menu", "sectorflow", "page:sectorflow", 6),
        ("市场数据", "个股资金流", "💰", "menu", "fundflow", "page:fundflow", 7),
        (None, "📡 iFinD数据", "📡", "dir", None, None, 3),
        ("📡 iFinD数据", "行情", "📋", "menu", "ifind-stocklist", "page:ifind-stocklist", 1),
        ("📡 iFinD数据", "K线数据", "📈", "menu", "ifind-kline", "page:ifind-kline", 2),
        ("📡 iFinD数据", "龙虎榜", "🐉", "menu", "ifind-lhb", "page:ifind-lhb", 3),
        ("📡 iFinD数据", "公告信息", "📜", "menu", "ifind-announce", "page:ifind-announce", 4),
        ("📡 iFinD数据", "资金流向", "💰", "menu", "ifind-fundflow", "page:ifind-fundflow", 5),
        ("📡 iFinD数据", "舆情/新闻", "📰", "menu", "newsense", "page:newsense", 6),
        ("📡 iFinD数据", "接口文档", "📖", "menu", "ifind-doc", "page:ifind-doc", 7),
        ("📡 iFinD数据", "数据仓库", "🗄", "menu", "ifind-warehouse", "page:ifind-warehouse", 8),
        (None, "专业区（调参研究，平时不用看）", "🧪", "dir", None, None, 4),
        ("专业区（调参研究，平时不用看）", "选股组合", "🧩", "menu", "combo", "page:combo", 1),
        ("专业区（调参研究，平时不用看）", "个股分析", "🔬", "menu", "single", "page:single", 2),
        ("专业区（调参研究，平时不用看）", "因子策略库", "🧮", "menu", "factorlib", "page:factorlib", 3),
        ("专业区（调参研究，平时不用看）", "选股工作台", "🪄", "menu", "picker", "page:picker", 4),
        ("专业区（调参研究，平时不用看）", "模拟交易", "📈", "menu", "trades", "page:trades", 5),
        ("专业区（调参研究，平时不用看）", "进化看板", "🧬", "menu", "evo", "page:evo", 6),
        ("专业区（调参研究，平时不用看）", "回测浏览", "📊", "menu", "backtest", "page:backtest", 7),
        ("专业区（调参研究，平时不用看）", "定时任务", "⏰", "menu", "sched", "page:sched", 8),
        (None, "系统", "⚙️", "dir", None, None, 5),
        ("系统", "设置", "⚙️", "menu", "settings", "page:settings", 1),
        (None, "系统管理", "🔐", "dir", None, None, 6),
        ("系统管理", "用户管理", "👥", "menu", "admin-users", "page:admin-users", 1),
        ("系统管理", "角色管理", "🎭", "menu", "admin-roles", "page:admin-roles", 2),
        ("系统管理", "菜单管理", "📑", "menu", "admin-menus", "page:admin-menus", 3),
        ("系统管理", "组织管理", "🏢", "menu", "admin-orgs", "page:admin-orgs", 4),
    ]


def _insert_menu_seed(c) -> None:
    """按 _MENU_SEED 插入目录/菜单（幂等：目录按 parent+name、菜单按 parent+url_path 判重）。"""
    pid: dict[str, int] = {None: 0}
    for parent, name, icon, mtype, url, perm, sort in _MENU_SEED:
        if mtype == "dir":
            row = c.execute("SELECT id FROM sys_menu WHERE parent_id=? AND name=? AND mtype='dir'",
                            (pid[parent], name)).fetchone()
            if row:
                pid[name] = row[0]
                continue
            cur = c.execute(
                "INSERT INTO sys_menu (parent_id, name, icon, mtype, sort, status, created_at) "
                "VALUES (?,?,?,?,?,1,?)", (pid[parent], name, icon, mtype, sort, _now()))
            pid[name] = cur.lastrowid
        else:
            row = c.execute("SELECT id FROM sys_menu WHERE parent_id=? AND url_path=?",
                            (pid[parent], url)).fetchone()
            if row:
                continue
            c.execute(
                """INSERT INTO sys_menu (parent_id, name, icon, url_path, mtype, perm_code,
                       sort, status, created_at)
                   VALUES (?,?,?,?,?,?,?,1,?)""",
                (pid[parent], name, icon, url, mtype, perm, sort, _now()))


def _ensure_menu_sync(c) -> None:
    """老库增量同步：补 _MENU_SEED 新增菜单，并保证超管角色始终绑定全部菜单。"""
    _insert_menu_seed(c)
    sup = c.execute("SELECT id FROM sys_role WHERE code='superadmin'").fetchone()
    if not sup:
        return
    all_ids = [r[0] for r in c.execute("SELECT id FROM sys_menu").fetchall()]
    bound = {r[0] for r in c.execute("SELECT menu_id FROM sys_role_menu WHERE role_id=?",
                                     (sup[0],)).fetchall()}
    c.executemany("INSERT OR IGNORE INTO sys_role_menu (role_id, menu_id) VALUES (?,?)",
                  [(sup[0], m) for m in all_ids if m not in bound])


if __name__ == "__main__":
    # 独立运行自检：建库 + 播种 + 认证闭环
    st = stats()
    print("统计:", st)
    u = authenticate("admin", "admin123")
    assert u, "admin/admin123 登录失败"
    print("登录成功:", u["username"], "| 组织:", u["org_id"])
    perms = get_user_perms(u["id"])
    print(f"权限: {len(perms['urls'])} 页面 / {len(perms['perms'])} 权限码")
    assert "admin-users" in perms["urls"] and "page:today" in perms["urls"]
    assert verify_password("admin123", u["salt"], u["password_hash"])
    assert not verify_password("wrong", u["salt"], u["password_hash"])
    print("自检通过 ✔")
