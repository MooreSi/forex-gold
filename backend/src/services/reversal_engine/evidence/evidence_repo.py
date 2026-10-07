"""SQL only: isolated WAL database, short connections, no ledger mutations."""
from __future__ import annotations
from contextlib import contextmanager
import json
import sqlite3
from pathlib import Path
from .observations import digest


class EvidenceStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS observations (
                  id INTEGER PRIMARY KEY, env TEXT NOT NULL, source TEXT NOT NULL,
                  kind TEXT NOT NULL, key TEXT NOT NULL, event_ts REAL NOT NULL,
                  available_at REAL NOT NULL, payload TEXT NOT NULL, fingerprint TEXT NOT NULL,
                  UNIQUE(env,source,kind,key,event_ts,fingerprint));
                CREATE INDEX IF NOT EXISTS evidence_asof ON observations(env,kind,available_at);
                CREATE TABLE IF NOT EXISTS health (
                  name TEXT PRIMARY KEY, state TEXT, detail TEXT, ts REAL);
                CREATE TABLE IF NOT EXISTS experiments (
                  id TEXT PRIMARY KEY, payload TEXT NOT NULL, remote_id TEXT,
                  exported INTEGER NOT NULL DEFAULT 0);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def put(self, env, event):
        return self.put_many(env, [event]) == 1

    def put_many(self, env, events):
        inserted = 0
        with self.connect() as db:
            for event in events:
                payload = json.dumps(event["payload"], sort_keys=True, allow_nan=False)
                cur = db.execute("INSERT OR IGNORE INTO observations "
                    "(env,source,kind,key,event_ts,available_at,payload,fingerprint) VALUES (?,?,?,?,?,?,?,?)",
                    (env, event["source"], event["kind"], event["key"], event["event_ts"],
                     event["available_at"], payload, digest(event["payload"])))
                inserted += cur.rowcount
        return inserted

    def by_kind(self, env, kind):
        with self.connect() as db:
            return self.decode(db.execute("SELECT * FROM observations WHERE env=? AND kind=? ORDER BY id",
                                          (env, kind)).fetchall())

    @staticmethod
    def decode(rows):
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def events(self, env, decision_ts):
        with self.connect() as db:
            return self.decode(db.execute("SELECT * FROM observations WHERE env=? AND available_at<=? "
                                         "ORDER BY available_at,id", (env, decision_ts)).fetchall())

    def context_events(self, env, decision_ts):
        with self.connect() as db:
            return self.decode(db.execute("SELECT * FROM (SELECT *, ROW_NUMBER() OVER "
                "(PARTITION BY source,kind,key ORDER BY available_at DESC,id DESC) AS latest "
                "FROM observations WHERE env=? AND available_at<=? "
                "AND kind IN ('calendar','calendar_batch','cme_book','futures_bar','broker_tick')) "
                "WHERE latest=1", (env, decision_ts)).fetchall())

    def labels(self, env):
        with self.connect() as db:
            return self.decode(db.execute("SELECT * FROM (SELECT *, ROW_NUMBER() OVER "
                "(PARTITION BY key ORDER BY available_at DESC,id DESC) AS latest "
                "FROM observations WHERE env=? AND kind='broker_label') WHERE latest=1 "
                "ORDER BY event_ts", (env,)).fetchall())

    def health(self, name, state, detail, ts):
        with self.connect() as db:
            db.execute("INSERT INTO health VALUES (?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
                       "state=excluded.state,detail=excluded.detail,ts=excluded.ts",
                       (name, state, detail, ts))

    def status(self):
        with self.connect() as db:
            return {r["name"]: dict(r) for r in db.execute("SELECT * FROM health")}

    def experiment(self, run):
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO experiments(id,payload) VALUES (?,?)",
                       (run["id"], json.dumps(run, sort_keys=True, allow_nan=False)))

    def runs(self, pending=False):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM experiments" + (" WHERE exported=0" if pending else "")).fetchall()
            return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def remote(self, run_id, remote_id, exported=False):
        with self.connect() as db:
            db.execute("UPDATE experiments SET remote_id=?,exported=? WHERE id=?",
                       (remote_id, int(exported), run_id))

    def prune_ticks(self, before):
        # Keep calendar revisions, broker execution evidence and experiments.
        with self.connect() as db:
            db.execute("DELETE FROM observations WHERE kind IN ('broker_tick','cme_book') "
                       "AND available_at<?", (before,))


def broker_rows(main_path, reversal_path):
    """Each handle is read-only and short-lived; ambiguity is reported by the caller."""
    def read(path, sql):
        db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in db.execute(sql)]
        finally:
            db.close()
    signals = read(reversal_path, "SELECT * FROM re_signals WHERE live_exec_status='executed'")
    trades = read(main_path, "SELECT * FROM vantage_simulated_trades WHERE mt5_ticket IS NOT NULL")
    partials = read(main_path, "SELECT * FROM vantage_partial_closes")
    snapshots = read(reversal_path, "SELECT * FROM re_decision_snapshots WHERE chosen=1")
    return signals, trades, partials, snapshots
