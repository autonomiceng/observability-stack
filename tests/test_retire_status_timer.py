"""Retiring the version 1 status timer. systemctl is a stub; no user manager is touched."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "retire-status-timer.sh"


class RetireStatusTimerTests(unittest.TestCase):
    def test_retirement_is_shellcheck_clean_and_idempotent_on_a_fake_home(self):
        # validate.sh requires shellcheck; a bare interpreter still runs the behaviour check.
        if shutil.which("shellcheck"):
            subprocess.run(["shellcheck", "--shell=sh", str(SCRIPT)], check=True)
        for present in (("observability-status.service", "observability-status.timer"),
                        ("observability-status.service",)):
            for state, env_name in (("", ".env"), ("'custom state'", ".env"), ("custom", "operator.env")):
                with self.subTest(present=present, state=state, env_name=env_name):
                    self.retire_twice(present, state, env_name)

    def retire_twice(self, present, state, env_name):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkout, home, bin_dir = root / "checkout", root / "home", root / "bin"
            (checkout / "scripts").mkdir(parents=True)
            shutil.copy(SCRIPT, checkout / "scripts")
            # Bootstrap saves OB_STATE_DIR; a relative value resolves from the checkout.
            (checkout / env_name).write_text(f"OB_STATE_DIR={state}\n" if state else "OB_ACCESS_MODE=local\n")
            data = checkout / (state.strip("'") or "data")
            units = home / ".config/systemd/user"
            units.mkdir(parents=True)
            for name in (*present, "other.timer"):
                (units / name).write_text("[Unit]\n")
            (data / "status").mkdir(parents=True)
            (data / "status/bootstrap.json").write_text("{}")
            (data / "status/observer.lock").write_text("")
            (data / "console").mkdir()
            for name in ("links.json", "alerts-degraded.json", "status.json"):
                (data / "console" / name).write_text("")
            bin_dir.mkdir()
            log = root / "systemctl.log"
            # Like systemctl, refuse a unit that has no file.
            (bin_dir / "systemctl").write_text(f"""#!/bin/sh
echo "$*" >> "{log}"
for unit in "$@"; do
  case "$unit" in *.timer|*.service) [ -e "{units}/$unit" ] || exit 1;; esac
done
""")
            (bin_dir / "systemctl").chmod(0o755)
            env = {"HOME": str(home), "PATH": f"{bin_dir}:/usr/bin:/bin"}
            argv = ["sh", str(checkout / "scripts/retire-status-timer.sh")]
            if env_name != ".env":
                argv.append(str(checkout / env_name))
            runs = [subprocess.run(argv, env=env,
                                   capture_output=True, text=True, check=True) for _ in range(2)]

            self.assertEqual(log.read_text().splitlines(), [
                "--user disable --now " + " ".join(sorted(present, reverse=True)),
                "--user daemon-reload"])
            self.assertEqual(sorted(p.name for p in units.iterdir()), ["other.timer"])
            self.assertFalse((data / "status").exists())
            self.assertEqual(sorted(p.name for p in (data / "console").iterdir()),
                             ["alerts-degraded.json", "links.json", "status.json"])
            self.assertIn("removed", runs[0].stdout)
            self.assertNotIn("removed", runs[1].stdout)
            self.assertIn("no observability-status units", runs[1].stdout)
            # An unreadable env file could name another installation's records; refuse before any change.
            missing = subprocess.run(["sh", str(checkout / "scripts/retire-status-timer.sh"), str(root / "absent.env")],
                                     env=env, capture_output=True, text=True)
            self.assertEqual((missing.returncode, missing.stdout), (1, ""))


if __name__ == "__main__":
    unittest.main()
