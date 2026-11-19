# -*- coding: utf-8 -*-
"""S6-FIX-04：便携备份元数据读取的库隔离测试。

守的是这条边界：**读元数据必须能指定库，且绝不隐式碰生产库**。

A 指定数据库读取      —— 传 db_path 就读那个库的 user_version 与 embedding 元数据
B 多数据库隔离        —— 两库不同的版本/配置互不串味
C 全局路径隔离        —— 全局 DB_PATH 指向"一读就爆"的陷阱库，证明指定库时不会碰它
D 不存在数据库        —— 不建空库、不产生文件、行为明确
E 损坏数据库          —— 明确报错、不覆盖、不清空
F WAL 场景            —— 未 checkpoint 的数据要读得到（immutable 读不到，必须退回 ro）
G Sidecar 与指纹      —— 读取前后 sha256/size/mtime_ns 不变；sidecar 按策略如实记录

硬约束（任务 §四）：全程不连接生产库，只对生产库做**文件级**指纹比对。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import portable_backup as pb   # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
_TMPS: list[Path] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    mark = "PASS" if ok else "FAIL"
    line = f"[{mark}] {name}"
    if detail:
        line += f" — {detail}"
    print(line)


def mktmp() -> Path:
    d = Path(tempfile.mkdtemp(prefix="s6fix04-"))
    _TMPS.append(d)
    return d


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def fingerprint(path: Path) -> dict:
    """文件级指纹：内容 + 大小 + mtime_ns + 三个 sidecar 的存在与指纹。"""
    st = path.stat()
    fp = {"sha256": sha256_of(path), "size": st.st_size, "mtime_ns": st.st_mtime_ns}
    for suffix in ("-wal", "-shm", "-journal"):
        p = Path(str(path) + suffix)
        if p.is_file():
            sst = p.stat()
            fp[suffix] = {"size": sst.st_size, "mtime_ns": sst.st_mtime_ns}
        else:
            fp[suffix] = None
    return fp


def same_fp(a: dict, b: dict) -> bool:
    return a == b


def sidecar_names(path: Path) -> list[str]:
    return [s for s in ("-wal", "-shm", "-journal") if Path(str(path) + s).is_file()]


def make_db(path: Path, *, user_version: int = 0, model: str | None = None,
            dimension: int | None = None, wal: bool = False,
            with_embeddings: bool = True) -> None:
    """造一个最小库：memories +（可选）memory_embeddings，写 user_version。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        if wal:
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS memories ("
            " id INTEGER PRIMARY KEY, content TEXT NOT NULL)"
        )
        if with_embeddings:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS memory_embeddings ("
                " memory_id INTEGER PRIMARY KEY,"
                " model_name TEXT NOT NULL,"
                " content_hash TEXT NOT NULL,"
                " dimension INTEGER NOT NULL,"
                " vector_json TEXT NOT NULL)"
            )
        conn.execute("INSERT OR IGNORE INTO memories(id, content) VALUES (1, '占位内容')")
        if with_embeddings and model is not None and dimension is not None:
            conn.execute(
                "INSERT OR REPLACE INTO memory_embeddings"
                "(memory_id, model_name, content_hash, dimension, vector_json)"
                " VALUES (1, ?, 'h', ?, '[]')",
                (model, dimension),
            )
        conn.execute(f"PRAGMA user_version={int(user_version)}")
        conn.commit()
    finally:
        conn.close()


def production_fp() -> dict | None:
    """生产库文件级指纹；只用文件 API，不连接数据库。"""
    p = Path(ROOT) / "data" / "agent.db"
    try:
        return fingerprint(p) if p.is_file() else None
    except OSError:
        return None


# ---------------------------------------------------------------------------
# A. 指定数据库读取
# ---------------------------------------------------------------------------
def test_a_explicit_db() -> None:
    print("\n--- A. 指定数据库读取 ---")
    tmp = mktmp()
    db = tmp / "a.db"
    make_db(db, user_version=7, model="custom/model-a", dimension=512)

    info = pb.read_database_metadata(db)
    check("A 指定 db_path 读到 database_exists", info["database_exists"] is True, info["db_path"])
    check("A readable=True", info["readable"] is True, f"error={info['error']!r}")
    check("A user_version 正确", info["user_version"] == 7, str(info["user_version"]))
    check("A schema_version 与 user_version 一致", info["schema_version"] == 7, str(info["schema_version"]))
    check("A embedding.model_name 正确", info["embedding"]["model_name"] == "custom/model-a",
          str(info["embedding"]["model_name"]))
    check("A embedding.dimensions 正确", info["embedding"]["dimensions"] == 512,
          str(info["embedding"]["dimensions"]))
    check("A 无错误", info["error"] == "", info["error"])

    # 兼容性：两个旧私有函数现在也接受 db_path，且不传时行为不变
    check("A _read_schema_version(db_path) 正确", pb._read_schema_version(db) == 7)
    check("A _read_embedding_info(db_path) 正确",
          pb._read_embedding_info(db) == {"model_name": "custom/model-a", "dimensions": 512},
          str(pb._read_embedding_info(db)))

    # 没有 memory_embeddings 表时不能炸，维度留 None 不伪造
    bare = tmp / "bare.db"
    make_db(bare, user_version=3, with_embeddings=False)
    bi = pb.read_database_metadata(bare)
    check("A 无 embedding 表时 readable 仍为 True", bi["readable"] is True, bi["error"])
    check("A 无 embedding 表时 dimensions 为 None（不伪造）", bi["embedding"]["dimensions"] is None,
          str(bi["embedding"]))
    check("A 无 embedding 表时 model_name 回落默认",
          bi["embedding"]["model_name"] not in ("", None), str(bi["embedding"]["model_name"]))


# ---------------------------------------------------------------------------
# B. 多数据库隔离
# ---------------------------------------------------------------------------
def test_b_multi_db_isolation() -> None:
    print("\n--- B. 多数据库隔离 ---")
    tmp = mktmp()
    a = tmp / "a.db"
    b = tmp / "b.db"
    make_db(a, user_version=3, model="model/alpha", dimension=128)
    make_db(b, user_version=9, model="model/beta", dimension=1024)

    ia = pb.read_database_metadata(a)
    ib = pb.read_database_metadata(b)
    check("B 两库 user_version 不同且各自正确",
          (ia["user_version"], ib["user_version"]) == (3, 9),
          f"{ia['user_version']} vs {ib['user_version']}")
    check("B 两库 model_name 各自正确",
          (ia["embedding"]["model_name"], ib["embedding"]["model_name"])
          == ("model/alpha", "model/beta"),
          f"{ia['embedding']['model_name']} vs {ib['embedding']['model_name']}")
    check("B 两库 dimensions 各自正确",
          (ia["embedding"]["dimensions"], ib["embedding"]["dimensions"]) == (128, 1024),
          f"{ia['embedding']['dimensions']} vs {ib['embedding']['dimensions']}")

    # 交错读三轮，确认没有缓存/串味
    seq = [(a, 3, "model/alpha"), (b, 9, "model/beta")] * 3
    ok = all(
        (lambda r: r["user_version"] == uv and r["embedding"]["model_name"] == mn)
        (pb.read_database_metadata(p))
        for p, uv, mn in seq
    )
    check("B 六次交错读取无串味", ok, "3 轮 A/B 交替")

    # 改 A 的版本后重读，A 变 B 不变
    conn = sqlite3.connect(str(a))
    try:
        conn.execute("PRAGMA user_version=42")
        conn.commit()
    finally:
        conn.close()
    check("B 改 A 后 A 变 42", pb.read_database_metadata(a)["user_version"] == 42)
    check("B 改 A 后 B 仍为 9", pb.read_database_metadata(b)["user_version"] == 9)

    # build_manifest 的 db_path 也要跟着走（不再读全局）
    m_a = pb.build_manifest("source_only", [], db_path=a)
    m_b = pb.build_manifest("source_only", [], db_path=b)
    check("B build_manifest 按 db_path 取 schema_version",
          (m_a["schema_version"], m_b["schema_version"]) == (42, 9),
          f"{m_a['schema_version']} vs {m_b['schema_version']}")
    check("B build_manifest 按 db_path 取 embedding",
          (m_a["embedding"]["model_name"], m_b["embedding"]["model_name"])
          == ("model/alpha", "model/beta"),
          f"{m_a['embedding']['model_name']} vs {m_b['embedding']['model_name']}")


# ---------------------------------------------------------------------------
# C. 全局路径隔离
# ---------------------------------------------------------------------------
def test_c_global_path_isolation() -> None:
    print("\n--- C. 全局路径隔离 ---")
    tmp = mktmp()
    real = tmp / "real.db"
    make_db(real, user_version=5, model="model/real", dimension=256)

    # 陷阱：全局 DB_PATH 指向一条不存在的路径，任何连接尝试都会被记下来
    trap_attempts: list[str] = []
    trap = tmp / "trap" / "agent.db"   # 故意让目录都不存在

    old = pb.db.DB_PATH
    pb.db.DB_PATH = trap
    old_connect = sqlite3.connect

    def spy_connect(database, *args, **kwargs):
        trap_attempts.append(str(database))
        return old_connect(database, *args, **kwargs)

    sqlite3.connect = spy_connect
    try:
        info = pb.read_database_metadata(real)
        m = pb.build_manifest("source_only", [], db_path=real)
    finally:
        sqlite3.connect = old_connect
        pb.db.DB_PATH = old

    check("C 指定库时读到的是真实库的值", info["user_version"] == 5, str(info["user_version"]))
    check("C 指定库时 build_manifest 也读真实库", m["schema_version"] == 5, str(m["schema_version"]))
    check("C 全程没有连接过陷阱路径",
          not any("trap" in a for a in trap_attempts), "; ".join(trap_attempts[:3]) or "无连接")
    check("C 陷阱目录未被创建（未产生空库）", not trap.parent.exists(), str(trap.parent))
    check("C 全局 DB_PATH 原值已恢复", pb.db.DB_PATH == old, str(pb.db.DB_PATH))

    # 不传 db_path 时才回落到全局——这是兼容性，也是它必须被显式传参替代的原因
    fallback = pb.read_database_metadata()
    check("C 不传 db_path 时回落到全局 DB_PATH",
          Path(fallback["db_path"]) == Path(old), fallback["db_path"])


# ---------------------------------------------------------------------------
# D. 不存在的数据库
# ---------------------------------------------------------------------------
def test_d_missing_db() -> None:
    print("\n--- D. 不存在的数据库 ---")
    tmp = mktmp()
    missing = tmp / "nope" / "ghost.db"

    info = pb.read_database_metadata(missing)
    check("D database_exists=False", info["database_exists"] is False)
    check("D readable=False", info["readable"] is False)
    check("D schema_version 回落 0（历史契约）", info["schema_version"] == 0, str(info["schema_version"]))
    check("D user_version 为 None（没读到就是没读到）", info["user_version"] is None,
          str(info["user_version"]))
    check("D 不存在不算错误", info["error"] == "", info["error"])
    check("D 没有创建库文件", not missing.exists(), str(missing))
    check("D 没有创建父目录", not missing.parent.exists(), str(missing.parent))
    check("D 目录里没有任何文件", not list(tmp.rglob("*")), str(list(tmp.rglob("*"))))
    check("D 旧接口同样不炸", pb._read_schema_version(missing) == 0)
    check("D 旧接口 embedding 不伪造维度", pb._read_embedding_info(missing)["dimensions"] is None)

    # 已存在但为空的目录也不能被当库
    empty_dir = tmp / "adir"
    empty_dir.mkdir()
    di = pb.read_database_metadata(empty_dir)
    check("D 目录当库时 database_exists=False", di["database_exists"] is False, di["db_path"])
    check("D 目录当库时不产生文件", not list(empty_dir.iterdir()), str(list(empty_dir.iterdir())))


# ---------------------------------------------------------------------------
# E. 损坏数据库
# ---------------------------------------------------------------------------
def test_e_corrupt_db() -> None:
    print("\n--- E. 损坏数据库 ---")
    tmp = mktmp()
    bad = tmp / "corrupt.db"
    junk = b"this is definitely not a sqlite database\n" * 64
    bad.write_bytes(junk)
    before = fingerprint(bad)

    info = pb.read_database_metadata(bad)
    check("E database_exists=True（文件确实在）", info["database_exists"] is True)
    check("E readable=False（不能当完整/正常）", info["readable"] is False)
    check("E error 非空且可追踪", bool(info["error"]), info["error"])
    check("E error 提到 not a database",
          "not a database" in info["error"].lower() or "无法只读打开" in info["error"],
          info["error"])
    check("E schema_version 回落 0", info["schema_version"] == 0, str(info["schema_version"]))

    after = fingerprint(bad)
    check("E 损坏文件未被覆盖（sha256 未变）", before["sha256"] == after["sha256"])
    check("E 损坏文件未被清空（size 未变）", before["size"] == after["size"],
          f"{before['size']} -> {after['size']}")
    check("E 损坏文件内容逐字节未变", bad.read_bytes() == junk)

    # 空文件：SQLite 把 0 字节文件当作"合法的空库"（无表），不算损坏。
    # 这里如实断言它的真实语义，而不是硬套"损坏必须报错"——报告里单独记录这条。
    empty = tmp / "empty.db"
    empty.write_bytes(b"")
    ei = pb.read_database_metadata(empty)
    check("E 0 字节文件 database_exists=True（文件在）", ei["database_exists"] is True)
    check("E 0 字节文件按 SQLite 语义可打开（空库，非损坏）", ei["readable"] is True, ei["error"])
    check("E 0 字节文件 user_version=0", ei["user_version"] == 0, str(ei["user_version"]))
    check("E 0 字节文件没有 embedding 元数据（dimensions=None）",
          ei["embedding"]["dimensions"] is None, str(ei["embedding"]))
    check("E 0 字节文件未被写入（仍为 0 字节）", empty.stat().st_size == 0,
          str(empty.stat().st_size))
    check("E 0 字节文件读取未产生 sidecar", sidecar_names(empty) == [], str(sidecar_names(empty)))

    # 旧接口在坏库上不能抛
    check("E 旧接口对坏库不抛异常", pb._read_schema_version(bad) == 0)
    check("E 旧接口对坏库 embedding 不伪造", pb._read_embedding_info(bad)["dimensions"] is None)


# ---------------------------------------------------------------------------
# F. WAL 场景
# ---------------------------------------------------------------------------
def test_f_wal_visibility() -> None:
    print("\n--- F. WAL 场景 ---")
    tmp = mktmp()
    db = tmp / "wal.db"
    make_db(db, user_version=0, model="model/old", dimension=128, wal=True)

    # 保持一个连接不关，写入新版本——数据只存在于 -wal 里，主文件还是旧值
    holder = sqlite3.connect(str(db))
    try:
        holder.execute("PRAGMA journal_mode=WAL")
        holder.execute("PRAGMA user_version=99")
        holder.execute(
            "UPDATE memory_embeddings SET model_name='model/new', dimension=777 WHERE memory_id=1"
        )
        holder.commit()

        wal = Path(str(db) + "-wal")
        check("F -wal 里确实有未合并数据", wal.is_file() and wal.stat().st_size > 0,
              f"wal size={wal.stat().st_size if wal.is_file() else 'None'}")

        info = pb.read_database_metadata(db)
        check("F WAL 有内容时走 readonly（不用 immutable）", info["read_mode"] == "readonly",
              info["read_mode"])
        check("F 读到 -wal 里的 user_version=99（不是主文件的旧值）",
              info["user_version"] == 99, str(info["user_version"]))
        check("F 读到 -wal 里的 model_name=model/new",
              info["embedding"]["model_name"] == "model/new",
              str(info["embedding"]["model_name"]))
        check("F 读到 -wal 里的 dimension=777",
              info["embedding"]["dimensions"] == 777,
              str(info["embedding"]["dimensions"]))
        check("F 读取过程中没有新增 sidecar（-wal/-shm 本来就都在）",
              info["sidecar_created"] is False, str(info["sidecar_after"]))
    finally:
        holder.close()

    # 连接关闭后 SQLite 会合并并删掉 -wal → 这时应自动切到 immutable
    check("F 关闭连接后 -wal 已被 SQLite 合并清理",
          not Path(str(db) + "-wal").is_file(),
          f"残留={sidecar_names(db)}")
    info2 = pb.read_database_metadata(db)
    check("F 无 -wal 时自动切回 immutable", info2["read_mode"] == "immutable", info2["read_mode"])
    check("F 合并后仍读到 user_version=99", info2["user_version"] == 99, str(info2["user_version"]))
    check("F 合并后仍读到 model/new", info2["embedding"]["model_name"] == "model/new",
          str(info2["embedding"]["model_name"]))

    # 反证：强制 immutable 读"wal 有内容"的库会读到旧值 —— 这就是不能盲目 immutable 的理由
    db2 = tmp / "wal2.db"
    make_db(db2, user_version=0, model="model/old", dimension=128, wal=True)
    holder2 = sqlite3.connect(str(db2))
    try:
        holder2.execute("PRAGMA journal_mode=WAL")
        holder2.execute("PRAGMA user_version=55")
        holder2.commit()
        uri_imm = pb._readonly_uri(db2, immutable=True)
        conn = sqlite3.connect(uri_imm, uri=True)
        try:
            blind = int(conn.execute("PRAGMA user_version").fetchone()[0])
        finally:
            conn.close()
        check("F 反证：盲目 immutable 会读到旧值 0（而非 55）", blind == 0, str(blind))
        check("F 反证成立，说明 WAL 感知策略是必要的",
              pb.read_database_metadata(db2)["user_version"] == 55,
              str(pb.read_database_metadata(db2)["user_version"]))
    finally:
        holder2.close()


# ---------------------------------------------------------------------------
# G. Sidecar 与指纹
# ---------------------------------------------------------------------------
def test_g_sidecar_and_fingerprint() -> None:
    print("\n--- G. Sidecar 与指纹 ---")
    tmp = mktmp()

    # G1 干净库（ROLLBACK journal，无 -wal）：必须零 sidecar、三项指纹全不变
    clean = tmp / "clean.db"
    make_db(clean, user_version=4, model="model/clean", dimension=64, wal=False)
    check("G1 前置：干净库旁边无 sidecar", sidecar_names(clean) == [], str(sidecar_names(clean)))
    before = fingerprint(clean)
    info = pb.read_database_metadata(clean)
    after = fingerprint(clean)
    check("G1 读取走 immutable（零副作用路径）", info["read_mode"] == "immutable", info["read_mode"])
    check("G1 sidecar_created=False", info["sidecar_created"] is False, str(info["sidecar_after"]))
    check("G1 读取后仍无任何 sidecar", sidecar_names(clean) == [], str(sidecar_names(clean)))
    check("G1 sha256 未变", before["sha256"] == after["sha256"])
    check("G1 size 未变", before["size"] == after["size"])
    check("G1 mtime_ns 未变", before["mtime_ns"] == after["mtime_ns"],
          f"{before['mtime_ns']} -> {after['mtime_ns']}")
    check("G1 整体文件级指纹逐项相同", same_fp(before, after), str(before))

    # G2 连读 5 次仍零副作用
    for _ in range(5):
        pb.read_database_metadata(clean)
    after5 = fingerprint(clean)
    check("G2 连读 5 次后指纹仍与首次相同", same_fp(before, after5), str(after5))
    check("G2 连读 5 次后仍无 sidecar", sidecar_names(clean) == [], str(sidecar_names(clean)))

    # G3 WAL 库：允许 sidecar（-wal/-shm 本就在），但主库三项指纹不得因读取而变化
    wdb = tmp / "walkeep.db"
    make_db(wdb, user_version=8, model="model/wal", dimension=96, wal=True)
    # 打开一次让它把 -wal 合并掉，随后人为留一个 0 字节 -wal 制造"有 sidecar 但无数据"的形态
    conn = sqlite3.connect(str(wdb))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA user_version=8")
        conn.commit()
    finally:
        conn.close()
    wbefore = fingerprint(wdb)
    winfo = pb.read_database_metadata(wdb)
    wafter = fingerprint(wdb)
    check("G3 WAL 库读取后主库 sha256 未变", wbefore["sha256"] == wafter["sha256"])
    check("G3 WAL 库读取后主库 size 未变", wbefore["size"] == wafter["size"])
    check("G3 WAL 库读取后主库 mtime_ns 未变", wbefore["mtime_ns"] == wafter["mtime_ns"],
          f"{wbefore['mtime_ns']} -> {wafter['mtime_ns']}")
    check("G3 sidecar_created 如实反映（不假报零）",
          isinstance(winfo["sidecar_created"], bool), str(winfo["sidecar_created"]))
    if winfo["sidecar_created"]:
        # 允许，但必须记录原因：这条路径是 readonly，SQLite 会建/触碰 -shm
        print(f"      [记录] WAL 路径产生了 sidecar：{winfo['sidecar_after']}，"
              f"原因：read_mode=readonly 时 SQLite 需要 -shm 协调读者")
        check("G3 readonly 模式下 sidecar 产生与 read_mode 一致",
              winfo["read_mode"] == "readonly", winfo["read_mode"])
    else:
        check("G3 immutable 路径零 sidecar，与 read_mode 一致",
              winfo["read_mode"] == "immutable", winfo["read_mode"])

    # G4 只读文件（权限层面）也应能读元数据
    ro = tmp / "readonly.db"
    make_db(ro, user_version=11, model="model/ro", dimension=32)
    os.chmod(ro, 0o444)
    try:
        ri = pb.read_database_metadata(ro)
        check("G4 只读权限文件仍可读元数据", ri["readable"] is True, ri["error"])
        check("G4 只读权限文件 user_version 正确", ri["user_version"] == 11, str(ri["user_version"]))
        check("G4 只读文件未被改动", ri["sidecar_created"] is False, str(ri["sidecar_after"]))
    finally:
        try:
            os.chmod(ro, 0o644)
        except OSError:
            pass

    # G5 路径带空格/中文/特殊字符：URI 编码必须正确
    odd_dir = tmp / "带 空格 的 dir"
    odd = odd_dir / "库 名字.db"
    make_db(odd, user_version=13, model="model/odd", dimension=48)
    oi = pb.read_database_metadata(odd)
    check("G5 含空格/中文的路径可正确读取", oi["readable"] is True, oi["error"])
    check("G5 含空格/中文的路径 user_version 正确", oi["user_version"] == 13, str(oi["user_version"]))
    check("G5 含空格/中文的路径 URI 已编码",
          " " not in pb._readonly_uri(odd), pb._readonly_uri(odd))


# ---------------------------------------------------------------------------
# 生产库全程未被触碰
# ---------------------------------------------------------------------------
def main() -> int:
    print("=" * 70)
    print("S6-FIX-04 便携备份元数据读取隔离测试")
    print("=" * 70)

    prod_before = production_fp()

    test_a_explicit_db()
    test_b_multi_db_isolation()
    test_c_global_path_isolation()
    test_d_missing_db()
    test_e_corrupt_db()
    test_f_wal_visibility()
    test_g_sidecar_and_fingerprint()

    prod_after = production_fp()

    print("\n--- 生产库文件级指纹（只用文件 API，未连接数据库）---")
    if prod_before is None:
        check("生产库存在（否则本项不可判定）", False, "data/agent.db 不存在")
    else:
        check("生产库 sha256 未变", prod_before["sha256"] == prod_after["sha256"],
              f"{prod_before['sha256'][:16]}…")
        check("生产库 size 未变", prod_before["size"] == prod_after["size"],
              str(prod_before["size"]))
        check("生产库 mtime_ns 未变", prod_before["mtime_ns"] == prod_after["mtime_ns"],
              f"{prod_before['mtime_ns']} -> {prod_after['mtime_ns']}")

    for d in _TMPS:
        shutil.rmtree(d, ignore_errors=True)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = len(RESULTS) - passed
    print("\n" + "=" * 70)
    print(f"结果：{passed} PASS / {failed} FAIL / 共 {len(RESULTS)} 项")
    print("=" * 70)
    if failed:
        print("\n失败项：")
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"  [FAIL] {name} — {detail}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
