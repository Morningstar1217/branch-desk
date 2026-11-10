"""Real Git integration checks, isolated under a temporary directory; no server/browser."""
import io
import json
import os
import shutil
import sys
from pathlib import Path
import subprocess
import tempfile
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
import unittest
from unittest.mock import Mock, patch

from server import GIT, GuardError, Handler, Repository, main, open_browser


class MergeChecks(unittest.TestCase):
    def setUp(self):
        self.resolver_patch = patch("server.ConflictResolver.start", return_value=None)
        self.resolver_start = self.resolver_patch.start()
        self.addCleanup(self.resolver_patch.stop)
        self.temp = tempfile.TemporaryDirectory(prefix="wt-merge-check-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name).resolve() / "repo with spaces 中文"
        self.path.mkdir()
        self.cmd("init", "-b", "target")
        self.cmd("config", "user.name", "Merge Test")
        self.cmd("config", "user.email", "test@example.invalid")
        self.cmd("config", "commit.gpgsign", "false")
        self.cmd("config", "core.hooksPath", str(Path(self.temp.name) / "empty-hooks"))
        (self.path / "common.txt").write_text("base\n")
        (self.path / ".gitignore").write_text("*.secret\n")
        self.commit("base")
        self.base = self.cmd("rev-parse", "HEAD")
        self.cmd("checkout", "-b", "incoming")
        (self.path / "incoming.txt").write_text("incoming\n")
        self.commit("incoming change")
        self.incoming = self.cmd("rev-parse", "HEAD")
        self.cmd("checkout", "target")
        self.repo = Repository(self.path)
        self.target = str(self.path)

    def cmd(self, *args):
        return subprocess.check_output([GIT, "-C", str(self.path), *args], stderr=subprocess.STDOUT).decode().strip()

    def commit(self, message):
        self.cmd("add", "-A")
        self.cmd("commit", "-m", message)

    def plan(self, ref="refs/heads/incoming"):
        return self.repo.preview(self.target, ref)

    def local_origin(self):
        remote = Path(self.temp.name) / "local-origin.git"
        subprocess.check_output([GIT, "clone", "--bare", "--no-hardlinks", self.target, str(remote)], stderr=subprocess.STDOUT)
        self.cmd("remote", "add", "origin", str(remote))
        return remote

    def test_fetch_updates_and_prunes_only_remote_tracking_refs(self):
        remote = self.local_origin()
        self.cmd("branch", "deleted", "target")
        self.cmd("update-ref", "refs/remotes/origin/deleted", self.base)
        (self.path / "common.txt").write_text("keep local edits")
        before = self.repo.snapshot(self.target)
        heads = self.cmd("show-ref", "--heads")
        remote_heads = subprocess.check_output([GIT, "--git-dir", str(remote), "show-ref", "--heads"])
        self.repo.fetch_remote()
        self.assertEqual(self.cmd("rev-parse", "refs/remotes/origin/incoming"), self.incoming)
        self.assertNotIn("refs/remotes/origin/deleted", self.cmd("show-ref"))
        self.assertEqual(self.cmd("show-ref", "--heads"), heads)
        self.assertEqual(self.repo.snapshot(self.target), before)
        self.assertEqual(subprocess.check_output([GIT, "--git-dir", str(remote), "show-ref", "--heads"]), remote_heads)
        subprocess.check_output([GIT, "--git-dir", str(remote), "update-ref", "-d", "refs/heads/incoming"])
        remote_heads = subprocess.check_output([GIT, "--git-dir", str(remote), "show-ref", "--heads"])
        self.repo.fetch_remote()
        self.assertNotIn("refs/remotes/origin/incoming", self.cmd("show-ref"))
        self.assertEqual(self.cmd("show-ref", "--heads"), heads)
        self.assertEqual(subprocess.check_output([GIT, "--git-dir", str(remote), "show-ref", "--heads"]), remote_heads)

    def test_fetch_preserves_local_branches_and_tags_with_custom_configuration(self):
        self.local_origin()
        self.cmd("branch", "master", "incoming")
        self.cmd("tag", "local-only", "target")
        self.cmd("config", "remote.origin.fetch", "+refs/heads/*:refs/heads/*")
        self.cmd("config", "fetch.pruneTags", "true")
        self.cmd("config", "remote.origin.pruneTags", "true")
        self.cmd("config", "remote.origin.tagOpt", "--tags")
        heads_and_tags = self.cmd("show-ref", "--heads", "--tags")
        self.repo.fetch_remote()
        self.assertEqual(self.cmd("show-ref", "--heads", "--tags"), heads_and_tags)
        self.assertEqual(self.cmd("rev-parse", "refs/remotes/origin/incoming"), self.incoming)

    def test_failed_fetch_invalidates_old_preview_without_changing_worktree(self):
        p = self.plan()
        before = self.repo.snapshot(self.target)
        with self.assertRaises(GuardError):
            self.repo.fetch_remote()
        self.assertEqual(self.repo.snapshot(self.target), before)
        with self.assertRaisesRegex(GuardError, "预览已失效"):
            self.repo.merge(p["plan"])

    def test_fetch_invalidates_successful_preview_and_checks_repository_identity(self):
        self.local_origin()
        p = self.plan()
        self.repo.fetch_remote()
        with self.assertRaisesRegex(GuardError, "预览已失效"):
            self.repo.merge(p["plan"])
        with patch.object(self.repo, "common_dir", return_value="/another/repository"):
            with self.assertRaisesRegex(GuardError, "仓库归属"):
                self.repo.fetch_remote()

    def test_fast_forward_stays_uncommitted_and_preserves_refs(self):
        # Trace2 consumers such as Git AI can update refs/notes independently.
        refs = self.cmd("for-each-ref", "--format=%(refname) %(objectname)", "refs/heads/", "refs/remotes/")
        p = self.plan()
        self.assertEqual(p["count"], 1)
        self.assertIn("incoming.txt", p["stat"])
        self.assertIn("incoming change", p["commits"])
        self.assertEqual(self.cmd("status", "--porcelain"), "")
        result = self.repo.merge(p["plan"])
        self.assertEqual(result["kind"], "pending")
        self.assertEqual(self.cmd("rev-parse", "HEAD"), self.base)
        self.assertEqual(self.cmd("for-each-ref", "--format=%(refname) %(objectname)", "refs/heads/", "refs/remotes/"), refs)
        self.assertEqual((self.path / "incoming.txt").read_text(), "incoming\n")
        self.assertIn("MERGE_HEAD", result["state"]["operations"])
        with self.assertRaises(GuardError):
            self.repo.merge(p["plan"])

    def test_conflicts_keep_both_sides_and_no_commit(self):
        self.cmd("checkout", "incoming")
        (self.path / "common.txt").write_text("source content\n")
        self.commit("source conflict")
        self.cmd("checkout", "target")
        (self.path / "common.txt").write_text("target content\n")
        self.commit("target conflict")
        head = self.cmd("rev-parse", "HEAD")
        result = self.repo.merge(self.plan()["plan"])
        self.assertEqual(result["kind"], "conflicts")
        self.assertEqual(result["state"]["conflicts"], ["common.txt"])
        content = (self.path / "common.txt").read_text()
        self.assertIn("target content", content)
        self.assertIn("source content", content)
        self.assertEqual(self.cmd("rev-parse", "HEAD"), head)
        self.resolver_start.assert_called_once()

    def test_master_can_be_source_and_local_merge_target_but_cannot_push(self):
        self.cmd("branch", "master", "incoming")
        self.assertEqual(self.plan("refs/heads/master")["count"], 1)
        self.cmd("branch", "-D", "master")
        self.cmd("checkout", "-b", "master", self.base)
        result = self.repo.merge(self.plan()["plan"])
        self.assertEqual(result["kind"], "pending")
        self.assertEqual(result["state"]["branch"], "master")
        self.assertEqual(self.cmd("rev-parse", "HEAD"), self.base)
        self.assertEqual(self.cmd("show", ":incoming.txt"), "incoming")
        with self.assertRaisesRegex(GuardError, "禁止推送 master"):
            self.repo.preview_push(self.target)

    def test_codex_conversation_uses_registered_worktree_and_fixed_prefill(self):
        with patch('server.open_codex') as open_codex:
            result = self.repo.open_codex_conversation(self.target, 'target')
        url = open_codex.call_args.args[0]
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        self.assertEqual((parsed.scheme, parsed.netloc), ('codex', 'new'))
        self.assertEqual(query, {'path': [str(self.path)], 'prompt': [
            f'请在已有工作区 {json.dumps(str(self.path), ensure_ascii=False)} 中处理，先核对该目录当前分支为「target」。\n'
            '打开「target」，进行调整：']})
        self.assertEqual(result, {'branch': 'target'})
        with patch('server.open_codex') as open_codex:
            with self.assertRaisesRegex(GuardError, '分支已变化'):
                self.repo.open_codex_conversation(self.target, 'master')
        open_codex.assert_not_called()
        with patch('server.open_codex') as open_codex:
            with self.assertRaisesRegex(GuardError, '登记的 worktree'):
                self.repo.open_codex_conversation(str(self.path.parent), 'target')
        open_codex.assert_not_called()

    def test_codex_worktree_conversation_reuses_main_project(self):
        worktree = self.path / '.aidp' / 'worktrees' / '需求 & 中文 #1'
        self.cmd('worktree', 'add', '--no-track', '-b', 'worktree-branch', str(worktree), 'incoming')
        before = self.repo.snapshot(self.target)
        prompt = (f'请在已有工作区 {json.dumps(str(worktree), ensure_ascii=False)} 中处理，'
                  '先核对该目录当前分支为「worktree-branch」。\n打开「worktree-branch」，进行调整：')
        for repository in (self.repo, Repository(worktree)):
            with self.subTest(launcher_root=repository.root), patch('server.open_codex') as open_codex:
                result = repository.open_codex_conversation(str(worktree), 'worktree-branch')
                query = parse_qs(urlsplit(open_codex.call_args.args[0]).query)
                self.assertEqual(query, {'path': [str(self.path)], 'prompt': [prompt]})
                self.assertEqual(result, {'branch': 'worktree-branch'})
        self.assertEqual(self.repo.snapshot(self.target), before)

    def test_cached_remote_source_without_network_or_ref_changes(self):
        self.cmd("remote", "add", "origin", "/does-not-exist")
        self.cmd("update-ref", "refs/remotes/origin/master", self.incoming)
        p = self.plan("refs/remotes/origin/master")
        self.assertEqual(self.repo.merge(p["plan"])["kind"], "pending")
        self.assertEqual(self.cmd("rev-parse", "refs/remotes/origin/master"), self.incoming)

    def test_dirty_untracked_staged_and_unstaged_blocked(self):
        (self.path / "new.txt").write_text("keep me")
        for stage in (False, True):
            if stage:
                self.cmd("add", "new.txt")
            with self.assertRaisesRegex(GuardError, "未跟踪"):
                self.repo.merge(self.plan()["plan"])
        self.cmd("reset", "--", "new.txt")
        (self.path / "new.txt").unlink()
        (self.path / "common.txt").write_text("keep edits")
        with self.assertRaises(GuardError):
            self.repo.merge(self.plan()["plan"])
        self.assertEqual((self.path / "common.txt").read_text(), "keep edits")

    def test_stale_source_is_blocked(self):
        p = self.plan()
        self.cmd("update-ref", "refs/heads/incoming", self.base)
        with self.assertRaisesRegex(GuardError, "已变化"):
            self.repo.merge(p["plan"])

    def test_stale_target_is_blocked(self):
        p = self.plan()
        (self.path / "other.txt").write_text("new target")
        self.commit("new target")
        with self.assertRaisesRegex(GuardError, "已变化"):
            self.repo.merge(p["plan"])

    def test_new_edits_after_preview_are_preserved(self):
        p = self.plan()
        (self.path / "common.txt").write_text("user changes")
        with self.assertRaises(GuardError):
            self.repo.merge(p["plan"])
        self.assertEqual((self.path / "common.txt").read_text(), "user changes")

    def test_upstream_must_have_same_remote_branch_name(self):
        self.cmd("remote", "add", "origin", "/does-not-exist")
        self.cmd("update-ref", "refs/remotes/origin/master", self.base)
        self.cmd("branch", "--set-upstream-to=origin/master", "target")
        with self.assertRaisesRegex(GuardError, "上游"):
            self.repo.merge(self.plan()["plan"])
        self.cmd("update-ref", "refs/remotes/origin/target", self.base)
        self.cmd("branch", "--set-upstream-to=origin/target", "target")
        self.assertEqual(self.repo.snapshot(self.target)["blocked"], "")

    def test_ignored_file_is_not_overwritten(self):
        self.cmd("checkout", "incoming")
        (self.path / "config.secret").write_text("incoming version")
        self.cmd("add", "-f", "config.secret")
        self.cmd("commit", "-m", "source tracks ignored file")
        self.cmd("checkout", "target")
        (self.path / "config.secret").write_text("local secret")
        with self.assertRaisesRegex(GuardError, "忽略路径"):
            self.repo.merge(self.plan()["plan"])
        self.assertEqual((self.path / "config.secret").read_text(), "local secret")

    def test_ignored_file_created_after_preview_is_preserved(self):
        self.cmd("checkout", "incoming")
        (self.path / "config.secret").write_text("incoming version")
        self.cmd("add", "-f", "config.secret")
        self.cmd("commit", "-m", "source tracks ignored file")
        self.cmd("checkout", "target")
        p = self.plan()
        (self.path / "config.secret").write_text("new local secret")
        with self.assertRaisesRegex(GuardError, "忽略路径"):
            self.repo.merge(p["plan"])
        self.assertEqual((self.path / "config.secret").read_text(), "new local secret")

    def test_ignored_directory_cannot_be_replaced_by_source_file(self):
        self.cmd("checkout", "incoming")
        (self.path / "folder.secret").write_text("incoming file")
        self.cmd("add", "-f", "folder.secret")
        self.cmd("commit", "-m", "source file")
        self.cmd("checkout", "target")
        (self.path / "folder.secret").mkdir()
        (self.path / "folder.secret" / "keep.txt").write_text("keep me")
        with self.assertRaisesRegex(GuardError, "忽略路径"):
            self.repo.merge(self.plan()["plan"])
        self.assertEqual((self.path / "folder.secret" / "keep.txt").read_text(), "keep me")

    def test_branches_include_unchecked_local_and_remote_only_branches(self):
        self.cmd("branch", "pre")
        self.cmd("branch", "master")
        self.local_origin()
        self.cmd("fetch", "origin")
        self.cmd("update-ref", "refs/remotes/origin/only-remote", self.incoming)
        branches = self.repo.overview()["branches"]
        by_name = {b["name"]: b for b in branches}
        self.assertEqual(by_name["pre"]["path"], "")
        self.assertEqual(by_name["master"]["path"], "")
        self.assertEqual(by_name["target"]["path"], self.target)
        self.assertEqual(by_name["origin/only-remote"]["remote"], "origin")
        self.assertNotIn("origin/pre", by_name)

    def husky_fixture(self, installed=True):
        hooks = self.path / ".husky"
        hooks.mkdir()
        (hooks / "pre-commit").write_text('#!/bin/sh\n. "$(dirname "$0")/_/husky.sh"\necho fixture-hook-ran\n')
        (hooks / "commit-msg").write_text('#!/bin/sh\n. "$(dirname "$0")/_/husky.sh"\n[ "$(cat "$1")" = "feat: fixture" ]\n')
        (hooks / "pre-commit").chmod(0o755)
        (hooks / "commit-msg").chmod(0o755)
        (self.path / "package.json").write_text(json.dumps({"devDependencies": {"husky": "8.0.0"}}))
        (self.path / ".gitignore").write_text("*.secret\nnode_modules/\n.aidp/\n")
        self.commit("add Husky fixture")
        self.cmd("config", "core.hooksPath", ".husky")
        self.cmd("branch", "pre")
        package = self.path / "node_modules/husky"
        if installed:
            (package / "lib").mkdir(parents=True)
            (package / "package.json").write_text('{"version":"8.0.0"}')
            (package / "husky.sh").write_text("# fixture Husky runtime\n")
            # The fake Node executable is Python; only this isolated installer uses it.
            (package / "lib/bin.js").write_text("""from pathlib import Path
import shutil,subprocess,sys
assert sys.argv[1:] == ['install']
p=Path('.husky/_');p.mkdir(parents=True,exist_ok=True)
(p/'.gitignore').write_text('*')
shutil.copyfile(Path(__file__).parents[1]/'husky.sh',p/'husky.sh')
subprocess.check_call(['git','config','core.hooksPath','.husky'])
""")
        return package

    def test_new_worktree_initializes_husky_and_keeps_normal_hooks_and_staging(self):
        self.husky_fixture()
        (self.path / "common.txt").write_text("user staged change")
        self.cmd("add", "common.txt")
        before = self.cmd("ls-files", "--stage"), self.cmd("diff", "--cached"), self.cmd("config", "--local", "--list")
        with patch("server.shutil.which", return_value=sys.executable):
            state = self.repo.prepare_target("refs/heads/pre")
        wt = Path(state["path"])
        self.assertTrue((wt / ".husky/_/husky.sh").is_file())
        self.assertEqual(state["status"], "")
        self.assertEqual((self.cmd("ls-files", "--stage"), self.cmd("diff", "--cached"), self.cmd("config", "--local", "--list")), before)
        output = subprocess.check_output([GIT, "-C", str(wt), "hook", "run", "pre-commit"], text=True, stderr=subprocess.STDOUT)
        self.assertIn("fixture-hook-ran", output)
        msg = Path(self.temp.name) / "message.txt"
        for value, expected in [("feat: fixture", 0), ("invalid", 1)]:
            msg.write_text(value)
            result = subprocess.run([GIT, "-C", str(wt), "hook", "run", "commit-msg", "--", str(msg)], capture_output=True)
            self.assertEqual(result.returncode, expected)
        (wt / ".husky/_/husky.sh").write_text("# keep user runtime\n")
        with patch("server.subprocess.run") as installer:
            self.repo.prepare_target("refs/heads/pre")
        installer.assert_not_called()
        self.assertEqual((wt / ".husky/_/husky.sh").read_text(), "# keep user runtime\n")

    def test_husky_missing_dependency_preserves_checkout_and_can_retry(self):
        package = self.husky_fixture()
        installer = (package / "lib/bin.js").read_text()
        shutil.rmtree(package)
        heads = self.cmd("show-ref", "--heads")
        with self.assertRaisesRegex(GuardError, "缺少 Node 或仓库已安装的 Husky"):
            self.repo.prepare_target("refs/heads/pre")
        wt = self.path / ".aidp/worktrees/pre"
        self.assertTrue(wt.is_dir())
        self.assertEqual(self.cmd("show-ref", "--heads"), heads)
        (wt / "user.txt").write_text("keep me")
        (package / "lib").mkdir(parents=True)
        (package / "package.json").write_text('{"version":"8.0.0"}')
        (package / "husky.sh").write_text("# fixture runtime\n")
        (package / "lib/bin.js").write_text(installer)
        with patch("server.shutil.which", return_value=sys.executable):
            self.repo.prepare_target("refs/heads/pre")
        self.assertEqual((wt / "user.txt").read_text(), "keep me")

    def test_husky_custom_configuration_is_not_overwritten(self):
        self.husky_fixture()
        self.cmd("config", "core.hooksPath", "custom-hooks")
        with self.assertRaisesRegex(GuardError, "自定义 core.hooksPath"):
            self.repo.prepare_target("refs/heads/pre")
        self.assertEqual(self.cmd("config", "--get", "core.hooksPath"), "custom-hooks")
        self.assertTrue((self.path / ".aidp/worktrees/pre").is_dir())
        self.cmd("config", "extensions.worktreeConfig", "true")
        wt = self.path / ".aidp/worktrees/pre"
        subprocess.check_call([GIT, "-C", str(wt), "config", "--worktree", "core.hooksPath", ".husky"])
        with self.assertRaisesRegex(GuardError, "自定义 core.hooksPath"):
            self.repo.prepare_target("refs/heads/pre")
        self.assertEqual(self.cmd("config", "--local", "--get", "core.hooksPath"), "custom-hooks")

    def test_husky_disabled_environment_does_not_skip_checks(self):
        self.husky_fixture()
        with patch.dict(os.environ, HUSKY="0"), self.assertRaisesRegex(GuardError, "禁用了 hooks"):
            self.repo.prepare_target("refs/heads/pre")
        self.assertFalse((self.path / ".aidp/worktrees/pre/.husky/_/husky.sh").exists())

    def test_husky_symlink_runtime_directory_is_not_followed(self):
        self.husky_fixture()
        wt = self.path / ".aidp/worktrees/pre"
        self.cmd("worktree", "add", str(wt), "pre")
        outside = Path(self.temp.name) / "outside-husky"
        outside.mkdir()
        (wt / ".husky/_").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(GuardError, "符号链接"):
            self.repo.prepare_target("refs/heads/pre")
        self.assertEqual(list(outside.iterdir()), [])

    def test_husky_installer_failure_keeps_checkout_and_reports_error(self):
        package = self.husky_fixture()
        (package / "lib/bin.js").write_text("raise SystemExit('fixture install failed')")
        with patch("server.shutil.which", return_value=sys.executable), self.assertRaisesRegex(GuardError, "fixture install failed"):
            self.repo.prepare_target("refs/heads/pre")
        self.assertTrue((self.path / ".aidp/worktrees/pre").is_dir())

    def test_prepare_local_target_preserves_existing_work_and_refs_and_reuses(self):
        self.cmd("branch", "pre")
        exclude = Path(self.cmd("rev-parse", "--path-format=absolute", "--git-path", "info/exclude"))
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text(".aidp/\n")
        (self.path / "common.txt").write_text("user staged edit")
        self.cmd("add", "common.txt")
        (self.path / "keep.txt").write_text("user untracked edit")
        before = self.cmd("status", "--porcelain"), self.cmd("diff", "--cached"), self.cmd("show-ref", "--heads")
        state = self.repo.prepare_target("refs/heads/pre")
        self.assertEqual(state["path"], str(self.path / ".aidp/worktrees/pre"))
        self.assertEqual(state["branch"], "pre")
        self.assertEqual(state["head"], self.base)
        self.assertEqual(state["status"], "")
        self.assertEqual(state["upstream"], "")
        self.assertEqual(self.repo.prepare_target("refs/heads/pre"), state)
        self.assertEqual(len(self.repo.worktrees()), 2)
        self.assertEqual((self.cmd("status", "--porcelain"), self.cmd("diff", "--cached"), self.cmd("show-ref", "--heads")), before)

    def test_prepare_remote_only_target_tracks_same_name_without_remote_writes(self):
        remote = self.local_origin()
        subprocess.check_call([GIT, "--git-dir", str(remote), "update-ref", "refs/heads/only-remote", self.incoming])
        self.cmd("fetch", "origin")
        remote_before = subprocess.check_output([GIT, "--git-dir", str(remote), "show-ref"])
        root_head = self.cmd("rev-parse", "HEAD")
        state = self.repo.prepare_target("refs/remotes/origin/only-remote")
        self.assertEqual(state["branch"], "only-remote")
        self.assertEqual(state["head"], self.incoming)
        self.assertEqual(state["upstream"], "refs/remotes/origin/only-remote")
        self.assertEqual(state["blocked"], "")
        self.assertEqual(self.cmd("rev-parse", "HEAD"), root_head)
        self.assertEqual(subprocess.check_output([GIT, "--git-dir", str(remote), "show-ref"]), remote_before)

    def test_prepare_target_blocks_invalid_refs_and_existing_paths(self):
        self.cmd("branch", "master")
        self.local_origin()
        self.cmd("fetch", "origin")
        self.cmd("branch", "pre")
        directory = self.path / ".aidp/worktrees/pre"
        directory.mkdir(parents=True)
        (directory / "keep.txt").write_text("keep")
        before = self.cmd("show-ref", "--heads"), self.cmd("worktree", "list", "--porcelain")
        for ref in ("refs/remotes/origin/master", "HEAD", None, {}, "refs/heads/pre"):
            with self.subTest(ref=ref), self.assertRaises(GuardError):
                self.repo.prepare_target(ref)
        self.assertEqual((directory / "keep.txt").read_text(), "keep")
        self.assertEqual((self.cmd("show-ref", "--heads"), self.cmd("worktree", "list", "--porcelain")), before)

    def test_prepare_master_target_creates_worktree_without_switching_root(self):
        self.cmd("branch", "master")
        state = self.repo.prepare_target("refs/heads/master")
        self.assertEqual(state["branch"], "master")
        self.assertEqual(state["blocked"], "")
        self.assertEqual(state["head"], self.base)
        self.assertEqual(self.cmd("branch", "--show-current"), "target")

    def test_prepare_target_rejects_symlink_parent_without_writing_outside(self):
        self.cmd("branch", "pre")
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (self.path / ".aidp").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(GuardError, "符号链接"):
            self.repo.prepare_target("refs/heads/pre")
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(len(self.repo.worktrees()), 1)

    def test_prepare_target_rejects_directory_inside_another_worktree(self):
        self.cmd("branch", "feature/child")
        directory = self.path / ".aidp/worktrees/feature"
        self.cmd("worktree", "add", "-b", "parent", str(directory), "target")
        with self.assertRaisesRegex(GuardError, "其他 worktree"):
            self.repo.prepare_target("refs/heads/feature/child")
        self.assertFalse((directory / "child").exists())
        self.assertEqual(len(self.repo.worktrees()), 2)

    def test_prepare_target_reuses_wrong_upstream_without_changing_configuration(self):
        self.local_origin()
        self.cmd("fetch", "origin")
        self.cmd("branch", "--set-upstream-to=origin/incoming", "target")
        config = self.cmd("config", "--local", "--list")
        state = self.repo.prepare_target("refs/heads/target")
        self.assertIn("同名", state["blocked"])
        self.assertEqual(self.cmd("config", "--local", "--list"), config)

    def test_linked_worktree_unicode_space_and_newline_paths(self):
        linked = Path(self.temp.name).resolve() / "linked 中文\nsecond line"
        self.cmd("worktree", "add", "-b", "linked", str(linked), "target")
        paths = [w["path"] for w in self.repo.worktrees()]
        self.assertIn(str(linked), paths)
        self.assertEqual(self.repo.snapshot(str(linked))["branch"], "linked")
        p = self.repo.preview(str(linked), "refs/heads/incoming")
        self.assertEqual(self.repo.merge(p["plan"])["kind"], "pending")
        self.assertEqual(self.cmd("status", "--porcelain"), "")

    def test_outside_target_and_arbitrary_source_rejected(self):
        for value in (None, {}, "/tmp", self.target + "/.."):
            with self.assertRaises(GuardError):
                self.repo.snapshot(value)
        for value in ("HEAD", "--help", "target; touch hacked", "refs/heads/no-such"):
            with self.assertRaises(GuardError):
                self.repo.preview(self.target, value)

    def test_detached_head_blocked(self):
        self.cmd("checkout", "--detach", "HEAD")
        with self.assertRaisesRegex(GuardError, "分离 HEAD"):
            self.repo.merge(self.plan()["plan"])

    def test_existing_merge_blocks_reentry(self):
        self.repo.merge(self.plan()["plan"])
        with self.assertRaisesRegex(GuardError, "Git 操作"):
            self.repo.merge(self.plan()["plan"])

    def test_no_new_commits_does_not_change_state(self):
        self.cmd("branch", "same", "target")
        p = self.plan("refs/heads/same")
        self.assertEqual(p["count"], 0)
        self.assertEqual(self.repo.merge(p["plan"])["kind"], "unchanged")
        self.assertEqual(self.cmd("status", "--porcelain"), "")

    def test_expired_plan_and_same_branch_rejected(self):
        p = self.plan()
        self.repo.plans[p["plan"]]["time"] = time.monotonic() - 601
        with self.assertRaises(GuardError):
            self.repo.merge(p["plan"])
        with self.assertRaises(GuardError):
            self.plan("refs/heads/target")


class RequestChecks(unittest.TestCase):
    def handler(self, headers):
        handler = Handler.__new__(Handler)
        handler.headers = headers
        handler.server = SimpleNamespace(server_port=8123, token="test-token")
        return handler

    def test_host_origin_and_fetch_site_are_checked(self):
        self.handler({"Host": "127.0.0.1:8123", "Origin": "http://127.0.0.1:8123"}).headers_ok()
        for headers in ({"Host": "evil.example:8123"},
                        {"Host": "127.0.0.1:8123", "Origin": "https://evil.example"},
                        {"Host": "127.0.0.1:8123", "Sec-Fetch-Site": "cross-site"}):
            with self.assertRaises(GuardError):
                self.handler(headers).headers_ok()

    def test_api_requires_token_and_json(self):
        for extra in ({}, {"X-Session-Token": "wrong"}, {"X-Session-Token": "test-token", "Content-Type": "text/plain"}):
            handler = self.handler({"Host": "127.0.0.1:8123", **extra})
            replies = []
            handler.reply = lambda *args: replies.append(args)
            handler.do_POST()
            self.assertEqual(replies[0][0], 400)

    def test_preferences_routes_require_token_and_use_current_repository(self):
        for token in ('wrong', 'test-token'):
            handler = self.handler({'Host': '127.0.0.1:8123', 'X-Session-Token': token})
            handler.path = '/api/preferences'
            handler.server.repo = SimpleNamespace(common='/fixture/common')
            replies = []
            handler.reply = lambda *args: replies.append(args)
            with patch('server.read_pinned', return_value=['refs/heads/pre']) as read:
                handler.do_GET()
            self.assertEqual(replies[0][0], 200 if token == 'test-token' else 403)
            self.assertEqual(read.call_count, int(token == 'test-token'))
        body = b'{"pinned":["refs/heads/pre"]}'
        handler = self.handler({'Host': '127.0.0.1:8123', 'X-Session-Token': 'test-token',
                                'Content-Type': 'application/json', 'Content-Length': str(len(body))})
        handler.rfile = io.BytesIO(body)
        handler.path = '/api/preferences'
        guard = Mock(side_effect=GuardError('batch running'))
        handler.server.repo = SimpleNamespace(common='/fixture/common', batch=SimpleNamespace(guard=guard))
        replies = []
        handler.reply = lambda *args: replies.append(args)
        with patch('server.write_pinned', return_value=['refs/heads/pre']) as write:
            handler.do_POST()
        write.assert_called_once_with('/fixture/common', ['refs/heads/pre'])

    def test_codex_new_route_uses_only_the_confirmed_worktree_and_branch(self):
        handler = self.handler({'Host': '127.0.0.1:8123', 'X-Session-Token': 'test-token',
                                'Content-Type': 'application/json'})
        body = json.dumps({'path': '/fixture/worktree', 'branch': 'M-1-需求'}).encode()
        handler.headers['Content-Length'] = str(len(body))
        handler.rfile = io.BytesIO(body)
        handler.path = '/api/codex/new'
        operation = Mock(return_value={'branch': 'M-1-需求'})
        batch_guard = Mock()
        handler.server.repo = SimpleNamespace(batch=SimpleNamespace(guard=batch_guard), open_codex_conversation=operation)
        handler.reply = Mock()
        handler.do_POST()
        operation.assert_called_once_with('/fixture/worktree', 'M-1-需求')
        handler.reply.assert_called_once_with(200, {'branch': 'M-1-需求'})
        batch_guard.assert_called_once_with()

    def test_valid_api_routes_without_binding_a_port(self):
        for route, result in (("overview", {"root": "fixture"}), ("fetch", {"output": "updated"})):
            with self.subTest(route=route):
                handler = self.handler({"Host": "127.0.0.1:8123", "X-Session-Token": "test-token",
                                        "Content-Type": "application/json", "Content-Length": "2"})
                handler.rfile = io.BytesIO(b"{}")
                handler.path = "/api/" + route
                handler.server.repo = SimpleNamespace(overview=lambda: result.copy(), fetch_remote=lambda: result, common='/fixture/common', root='/fixture')
                handler.server.projects = SimpleNamespace(current=lambda _: ('fixture-id', {'name': 'fixture'}))
                handler.server.schedule = SimpleNamespace(config={'enabled': False})
                replies = []
                handler.reply = lambda *args: replies.append(args)
                handler.do_POST()
                if route == 'overview':
                    self.assertEqual(replies[0][0], 200)
                    self.assertEqual(replies[0][1]['root'], result['root'])
                    self.assertEqual(replies[0][1]['project']['name'], 'fixture')
                else:
                    self.assertEqual(replies, [(200, result)])

    def test_service_status_is_read_only_but_start_keeps_batch_guard(self):
        for route in ('status', 'start', 'stop/preview', 'stop'):
            with self.subTest(route=route):
                body = b'{"path":"/fixture","branch":"feature","backend":""}'
                handler = self.handler({"Host": "127.0.0.1:8123", "X-Session-Token": "test-token",
                                        "Content-Type": "application/json", "Content-Length": str(len(body))})
                handler.rfile = io.BytesIO(body)
                handler.path = "/api/services/" + route
                guard = Mock(side_effect=GuardError('batch running'))
                handler.server.repo = SimpleNamespace(batch=SimpleNamespace(guard=guard))
                handler.server.services = Mock()
                handler.server.services.status.return_value = {'items': {}, 'error': ''}
                replies = []
                handler.reply = lambda *args: replies.append(args)
                handler.do_POST()
                if route == 'status':
                    self.assertEqual(replies, [(200, {'items': {}, 'error': ''})])
                    guard.assert_not_called()
                else:
                    self.assertEqual(replies, [(400, {'error': 'batch running'})])
                    method = {'start': 'start', 'stop/preview': 'preview_stop', 'stop': 'stop'}[route]
                    getattr(handler.server.services, method).assert_not_called()
                    guard.side_effect = None
                    handler.rfile = io.BytesIO(body)
                    handler.do_POST()
                    getattr(handler.server.services, method).assert_called_once_with(json.loads(body))

    def test_schedule_routes_use_same_token_protection_without_binding_a_port(self):
        for route in ('overview', 'detail', 'link', 'preview', 'save'):
            with self.subTest(route=route):
                handler = self.handler({"Host": "127.0.0.1:8123", "X-Session-Token": "test-token",
                                        "Content-Type": "application/json", "Content-Length": "2"})
                handler.rfile = io.BytesIO(b"{}")
                handler.path = "/api/schedule/" + route
                handler.server.repo = object()
                with patch("server.FeishuProject") as service:
                    handler.server.schedule = service.return_value
                    service.return_value.handle.return_value = {"fixture": True}
                    replies = []
                    handler.reply = lambda *args: replies.append(args)
                    handler.do_POST()
                    service.return_value.handle.assert_called_once_with(route, {})
                    self.assertEqual(replies, [(200, {"fixture": True})])

    def test_removed_schedule_routes_are_rejected_without_mcp_calls(self):
        from feishu_project import FeishuProject
        for route in ('todos', 'unscheduled'):
            with self.subTest(route=route):
                handler = self.handler({"Host": "127.0.0.1:8123", "X-Session-Token": "test-token",
                                        "Content-Type": "application/json", "Content-Length": "2"})
                handler.rfile = io.BytesIO(b"{}")
                handler.path = "/api/schedule/" + route
                handler.server.repo = object()
                client = Mock()
                handler.server.schedule = FeishuProject(client)
                replies = []
                handler.reply = lambda *args: replies.append(args)
                handler.do_POST()
                self.assertEqual(replies[0][0], 400)
                client.call.assert_not_called()

    def test_schedule_script_asset_is_served_without_binding_a_port(self):
        handler = self.handler({"Host": "127.0.0.1:8123"})
        handler.path = "/schedule.js"
        replies = []
        handler.reply = lambda *args: replies.append(args)
        handler.do_GET()
        self.assertEqual(replies[0][0], 200)
        self.assertIn(b"const SchedulePanel", replies[0][1])
        self.assertEqual(replies[0][2], "text/javascript; charset=utf-8")

    def test_favicon_is_served_without_binding_a_port(self):
        handler = self.handler({"Host": "127.0.0.1:8123"})
        handler.path = "/favicon.svg"
        replies = []
        handler.reply = lambda *args: replies.append(args)
        handler.do_GET()
        self.assertEqual(replies[0][0], 200)
        self.assertIn(b'<svg xmlns="http://www.w3.org/2000/svg"', replies[0][1])
        self.assertEqual(replies[0][2], "image/svg+xml")

    def test_development_routes_without_launching_a_real_task(self):
        for route in ('start', 'status'):
            handler = self.handler({"Host": "127.0.0.1:8123", "X-Session-Token": "test-token",
                                    "Content-Type": "application/json", "Content-Length": "2"})
            handler.rfile = io.BytesIO(b"{}")
            handler.path = "/api/development/" + route
            handler.server.repo = object()
            replies = []
            handler.reply = lambda *args: replies.append(args)
            with patch('server.DevelopmentLauncher') as service:
                handler.server.development = service.return_value
                handler.server.schedule = SimpleNamespace(config={'enabled': True}, ensure_enabled=lambda: None)
                method = getattr(service.return_value, route)
                method.return_value = {"fixture": True}
                handler.do_POST()
                method.assert_called_once_with(*(({},) if route == 'start' else ()))
                self.assertEqual(replies, [(200, {"fixture": True})])


class BrowserChecks(unittest.TestCase):
    def test_open_browser_passes_complete_session_url(self):
        with patch("server.sys.platform", "darwin"), patch("server.subprocess.run") as opener, patch("server.print") as output:
            open_browser("http://127.0.0.1:8123/#fixture-token")
            opener.assert_called_once_with(["/usr/bin/open", "http://127.0.0.1:8123/#fixture-token"], check=True, timeout=10)
            output.assert_not_called()

    def test_non_macos_uses_standard_browser_opener(self):
        with patch("server.sys.platform", "linux"), patch("server.webbrowser.open", return_value=True) as opener:
            open_browser("http://127.0.0.1:8123/#fixture-token")
            opener.assert_called_once_with("http://127.0.0.1:8123/#fixture-token", new=2)

    def test_browser_failure_keeps_manual_url_fallback(self):
        for failure in (OSError("unavailable"), subprocess.TimeoutExpired("open",10), subprocess.CalledProcessError(1,"open")):
            with patch("server.sys.platform", "darwin"), patch("server.subprocess.run", side_effect=failure), patch("server.print") as output:
                open_browser("http://127.0.0.1:8123/#fixture-token")
                output.assert_called_once()

    def test_main_opens_browser_in_background_after_binding_without_real_server(self):
        with patch("sys.argv", ["server.py", "--repo", "/fixture"]), patch("server.Repository"), \
             patch("server.RuntimeSession"), \
             patch("server.ProjectRegistry"), patch("server.history_file"), patch("server.service_options", return_value={'preset': 'bibase'}), \
             patch("server.WorktreeServices"), \
             patch("server.OperationHistory") as history_factory, \
             patch("server.HTTPServer") as server_factory, patch("server.threading.Thread") as thread_factory, \
             patch("server.secrets.token_urlsafe", return_value="fixture-token"), patch("server.print"):
            server = server_factory.return_value
            server.server_port = 8123
            server.serve_forever.side_effect = KeyboardInterrupt
            main()
            history_factory.assert_called_once()
            thread_factory.assert_called_once_with(target=open_browser, args=("http://127.0.0.1:8123/#fixture-token",), daemon=True)
            thread_factory.return_value.start.assert_called_once()
            server.serve_forever.assert_called_once()
            server.server_close.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
