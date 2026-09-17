"""Explicit A/B/A experiments; transient one-launch plans never edit bot data."""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import uuid

from . import proc
from .analysis import compare, startup_report


class Experiments:
    def __init__(self, observer):
        self.observer = observer
        self.config = observer.config
        self.lock = threading.Lock()
        self.cancelled = threading.Event()
        self.worker = None
        # A killed backend never silently resumes disruptive work.
        interrupted = []
        for job in observer.store.jobs(100):
            if job.get("state") in {"queued", "running", "restoring"}:
                job.update(
                    state="interrupted",
                    message="后端重启，实验未继续；下一次启动使用正常配置。",
                )
                observer.store.save_job(job)
                interrupted.append(job)
        self.clear_plan()
        for run in observer.store.runs(1):
            plan = run.get("plan") or {}
            ident = proc.identity(run["pid"])
            matching = next(
                (j for j in interrupted if j["id"] == plan.get("job_id")), None
            )
            if (
                matching
                and plan.get("mode") in {"skip", "disable"}
                and ident
                and ident[1] == run["start_ticks"]
            ):
                self.worker = threading.Thread(
                    target=self.recover, args=(matching,), daemon=True
                )
                self.worker.start()

    def recover(self, job):
        try:
            job["restoration"] = "restoring_after_observer_restart"
            self.observer.store.save_job(job)
            self.restore_normal()
            job["restoration"] = "normal_ready"
        except Exception as exc:  # noqa: BLE001 - isolate diagnostics/control failures
            job["restoration"] = "failed: " + type(exc).__name__
        self.observer.store.save_job(job)

    def restore_normal(self):
        previous = (self.observer.current_run or {}).get("id")
        self.clear_plan()
        self.restart()
        self.wait_for(
            lambda: (
                (self.observer.current_run or {}).get("ready_at")
                and (self.observer.current_run or {}).get("id") != previous
            ),
            self.config.ready_timeout,
            restoring=True,
        )

    def clear_plan(self):
        path = self.config.plan_path
        if path.exists():
            self.write_plan({"consumed": True, "expires_at": 0, "mode": "normal"})

    def write_plan(self, plan):
        path = self.config.plan_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8") as stream:
            os.chmod(temp, 0o600)
            json.dump(plan, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)

    def create(self, request: dict) -> dict:
        if self.config.cgroup:
            raise ValueError(
                "显式容器 cgroup 模式仅支持观察，不能执行 systemd 重启实验"
            )
        if not self.config.allow_experiments:
            raise ValueError("后端未启用重启实验（allow_experiments）")
        if request.get("confirm_restart") is not True:
            raise ValueError("需要明确确认重启实验")
        plugin = request.get("plugin", "")
        mode = request.get("mode", "skip")
        rounds = request.get("rounds", 1)
        if not isinstance(plugin, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", plugin):
            raise ValueError("插件目录名无效")
        if plugin == "astrbot_plugin_memory_scope":
            raise ValueError("不能在自动实验中排除观测插件自身")
        if (
            mode not in {"skip", "disable"}
            or type(rounds) is not int
            or not 1 <= rounds <= 3
        ):
            raise ValueError("模式必须是 skip/disable，轮数为 1..3")
        run = self.observer.current_run or {}
        inventory = run.get("inventory", [])
        chosen = [
            p
            for p in inventory
            if p.get("root_dir_name") == plugin
            and p.get("activated")
            and not p.get("reserved")
        ]
        if len(chosen) != 1:
            raise ValueError("请选择当前已启用的普通插件；请先同步插件清单")
        if (
            not run.get("probe_enabled")
            or not run.get("probe_finished_at")
            or not run.get("fingerprint")
        ):
            raise ValueError("需先通过启动探针成功启动一次，验证加载器适配与环境指纹")
        # A finish marker also occurs when an unsupported loader detaches.
        # Check the small event ledger, without loading the full sample history.
        evidence = startup_report(
            run, [], self.observer.store.rows("events", run["id"], 50000)
        )
        if not evidence["probe_complete"] or not evidence["phases"]:
            raise ValueError("启动探针记录不完整或适配失败，不能执行自动重启实验")
        with self.lock:
            if self.worker and self.worker.is_alive():
                raise ValueError("已有实验正在运行")
            self.cancelled.clear()
            job = {
                "id": uuid.uuid4().hex,
                "started_at": time.time(),
                "plugin": plugin,
                "mode": mode,
                "rounds": rounds,
                "state": "queued",
                "runs": [],
                "planned_restarts": rounds * 2 + 1,
                "original_run": run["id"],
                "fingerprint": run["fingerprint"],
            }
            self.observer.store.save_job(job)
            self.worker = threading.Thread(
                target=self.execute, args=(job,), daemon=True
            )
            self.worker.start()
            return dict(job)

    def cancel(self):
        self.cancelled.set()
        return {
            "requested": True,
            "message": "已请求停止；若处于测试配置，将恢复正常启动。",
        }

    def restart(self):
        result = subprocess.run(
            ["systemctl", "restart", self.config.unit],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if result.returncode:
            raise RuntimeError("systemctl restart failed")

    def wait_for(self, predicate, timeout, *, restoring=False):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not restoring and (
                self.cancelled.is_set() or self.observer.stop.is_set()
            ):
                raise InterruptedError("Experiment cancelled")
            value = predicate()
            if value:
                return value
            time.sleep(0.25)
        raise TimeoutError("Experiment timed out")

    def execute(self, job):
        self.cancelled.wait(
            1.0
        )  # Allow the creating HTTP request to finish before a restart.
        needs_restore = False
        try:
            sequence = ["A"] + [
                label for _ in range(job["rounds"]) for label in ("B", "A")
            ]
            for index, label in enumerate(sequence):
                if self.cancelled.is_set():
                    raise InterruptedError("Experiment cancelled")
                job.update(state="running", step=index + 1, label=label)
                self.observer.store.save_job(job)
                previous = (self.observer.current_run or {}).get("id")
                mode = job["mode"] if label == "B" else "normal"
                self.write_plan(
                    {
                        "job_id": job["id"],
                        "plugin": job["plugin"],
                        "mode": mode,
                        "label": label,
                        "expires_at": time.time() + self.config.ready_timeout + 60,
                    }
                )
                # Once a B launch was requested, any failure requires a normal recovery.
                needs_restore = needs_restore or label == "B"
                self.restart()

                def fresh_ready(previous=previous):
                    current = self.observer.current_run or {}
                    return (
                        dict(current)
                        if current.get("id") != previous
                        and current.get("ready_at")
                        and current.get("probe_finished_at")
                        else None
                    )

                run = self.wait_for(fresh_ready, self.config.ready_timeout)
                if (
                    run.get("plan", {}).get("job_id") != job["id"]
                    or run.get("plan", {}).get("label") != label
                ):
                    raise RuntimeError("Launcher did not consume experiment plan")
                if label == "B" and not run.get("plan_applied"):
                    raise RuntimeError("Plugin exclusion was not applied")
                if run.get("fingerprint") != job["fingerprint"]:
                    raise RuntimeError(
                        "Configuration or plugin versions changed during experiment"
                    )
                if label == "A":
                    needs_restore = False
                finish = (
                    run["ready_at"]
                    + self.config.settle_seconds
                    + self.config.observe_seconds
                )
                self.wait_for(
                    lambda finish=finish: (
                        (self.observer.latest or {}).get("ts", 0) >= finish
                    ),
                    self.config.settle_seconds + self.config.observe_seconds + 30,
                )
                if (self.observer.current_run or {}).get("id") != run["id"]:
                    raise RuntimeError("Unexpected restart during observation")
                job["runs"].append(
                    {
                        "label": label,
                        "id": run["id"],
                        "summary": self.observer.summary(run["id"]),
                    }
                )
                self.observer.store.save_job(job)
            groups = {
                label: [r["summary"] for r in job["runs"] if r["label"] == label]
                for label in ("A", "B")
            }
            job.update(state="complete", result=compare(groups), ended_at=time.time())
        except Exception as exc:  # noqa: BLE001 - isolate diagnostics/control failures
            job.update(
                state="cancelled" if isinstance(exc, InterruptedError) else "failed",
                message=str(exc),
                ended_at=time.time(),
            )
        finally:
            self.clear_plan()
            if needs_restore:
                try:
                    self.restore_normal()
                    job["restoration"] = "normal_ready"
                except Exception as exc:  # noqa: BLE001 - isolate diagnostics/control failures
                    job["restoration"] = "failed: " + type(exc).__name__
            self.observer.store.save_job(job)
