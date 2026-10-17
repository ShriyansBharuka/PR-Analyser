"""SQLite persistence: knowledge-base facts, issues, votes, candidates, verifications, approvals."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (id INTEGER PRIMARY KEY, commit_sha TEXT, kind TEXT, subject TEXT,
    claim TEXT, source_type TEXT, source TEXT, line INTEGER, status TEXT, priority INTEGER,
    data TEXT, collected_at TEXT);
CREATE TABLE IF NOT EXISTS issues (issue_id TEXT PRIMARY KEY, data TEXT, approval_status TEXT,
    created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS votes (id INTEGER PRIMARY KEY, stage TEXT, issue_id TEXT, voter TEXT,
    data TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS candidates (issue_id TEXT, cand_id TEXT, data TEXT, created_at TEXT,
    PRIMARY KEY (issue_id, cand_id));
CREATE TABLE IF NOT EXISTS verifications (id INTEGER PRIMARY KEY, issue_id TEXT, label TEXT,
    data TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS approvals (id INTEGER PRIMARY KEY, issue_id TEXT, gate TEXT,
    decision TEXT, note TEXT, actor TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, data TEXT, updated_at TEXT);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row(r):
    d = dict(r)
    d["data"] = json.loads(d["data"])
    return d


class DB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def _x(self, sql, args=()):
        with self.conn:
            return self.conn.execute(sql, args)

    # key/value: latest index, baseline, health, voter weights ...
    def put(self, key, data):
        self._x("INSERT OR REPLACE INTO kv VALUES (?,?,?)", (key, json.dumps(data), now()))

    def get(self, key, default=None):
        row = self._x("SELECT data FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["data"]) if row else default

    # knowledge base
    def replace_facts(self, commit, facts):
        with self.conn:
            self.conn.execute("DELETE FROM facts WHERE commit_sha=?", (commit,))
            self.conn.executemany(
                "INSERT INTO facts (commit_sha, kind, subject, claim, source_type, source, line, status,"
                " priority, data, collected_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [(commit, f["kind"], f["subject"], f["claim"], f["source_type"], f.get("source"),
                  f.get("line"), f["status"], f["priority"], json.dumps(f.get("data", {})), now())
                 for f in facts])

    def facts(self, commit=None, subject=None, kind=None):
        sql, args = "SELECT * FROM facts WHERE 1=1", []
        for col, val in (("commit_sha", commit), ("subject", subject), ("kind", kind)):
            if val is not None:
                sql += f" AND {col}=?"
                args.append(val)
        return [_row(r) for r in self._x(sql + " ORDER BY priority, id", args)]

    # issues
    def save_issue(self, issue: dict):
        old = self.issue(issue["issue_id"])
        created = old["created_at"] if old else issue.get("created_at", now())
        self._x("INSERT OR REPLACE INTO issues VALUES (?,?,?,?,?)",
                (issue["issue_id"], json.dumps(issue), issue["approval_status"], created, now()))

    def issue(self, issue_id):
        row = self._x("SELECT data FROM issues WHERE issue_id=?", (issue_id,)).fetchone()
        return json.loads(row["data"]) if row else None

    def issues(self):
        return [json.loads(r["data"]) for r in self._x("SELECT data FROM issues ORDER BY issue_id")]

    # council records
    def add_vote(self, stage, issue_id, voter, data):
        self._x("INSERT INTO votes (stage, issue_id, voter, data, created_at) VALUES (?,?,?,?,?)",
                (stage, issue_id, voter, json.dumps(data), now()))

    def votes(self, issue_id=None, stage=None):
        sql, args = "SELECT * FROM votes WHERE 1=1", []
        if issue_id:
            sql += " AND issue_id=?"
            args.append(issue_id)
        if stage:
            sql += " AND stage=?"
            args.append(stage)
        return [_row(r) for r in self._x(sql + " ORDER BY id", args)]

    def save_candidate(self, issue_id, cand):
        self._x("INSERT OR REPLACE INTO candidates VALUES (?,?,?,?)",
                (issue_id, cand["cand_id"], json.dumps(cand), cand.get("created_at", now())))

    def candidates(self, issue_id):
        return [json.loads(r["data"]) for r in
                self._x("SELECT data FROM candidates WHERE issue_id=? ORDER BY created_at, cand_id", (issue_id,))]

    def add_verification(self, issue_id, label, data):
        self._x("INSERT INTO verifications (issue_id, label, data, created_at) VALUES (?,?,?,?)",
                (issue_id, label, json.dumps(data), now()))

    def verifications(self, issue_id):
        return [_row(r) for r in self._x("SELECT * FROM verifications WHERE issue_id=? ORDER BY id", (issue_id,))]

    def archived_candidates(self, issue_id):
        """Candidates from earlier, archived repair runs of the same issue."""
        return [json.loads(r["data"]) for r in
                self._x("SELECT data FROM candidates WHERE issue_id LIKE ?", (f"{issue_id}@archived-%",))]

    def archive_repair_run(self, issue_id):
        """Move a previous repair run's candidates/reviews/verifications aside so a new run starts clean."""
        if not self.candidates(issue_id):
            return None
        tag = f"{issue_id}@archived-{now()}"
        with self.conn:
            self.conn.execute("UPDATE candidates SET issue_id=? WHERE issue_id=?", (tag, issue_id))
            self.conn.execute("UPDATE votes SET issue_id=? WHERE issue_id=? AND stage='repair-review'", (tag, issue_id))
            self.conn.execute("UPDATE verifications SET issue_id=? WHERE issue_id=?", (tag, issue_id))
        return tag

    # human approvals (gate = "issue" | "publication")
    def record_approval(self, issue_id, gate, decision, note="", actor="human-cli"):
        self._x("INSERT INTO approvals (issue_id, gate, decision, note, actor, created_at) VALUES (?,?,?,?,?,?)",
                (issue_id, gate, decision, note, actor, now()))

    def approvals(self, issue_id, gate=None):
        sql, args = "SELECT * FROM approvals WHERE issue_id=?", [issue_id]
        if gate:
            sql += " AND gate=?"
            args.append(gate)
        return [dict(r) for r in self._x(sql + " ORDER BY id", args)]

    def latest_decision(self, issue_id, gate):
        rows = self.approvals(issue_id, gate)
        return rows[-1]["decision"] if rows else None
