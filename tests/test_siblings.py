import subprocess

from glevels import siblings


def make(root, files):
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    return root


def run(repo, name, capsys):
    code = siblings.main([str(repo), "--as", name])
    return code, capsys.readouterr().out


APPLE = {
    "project.yml": "packages:\n  GStudioKit:\n    path: Kit\n  GDesign:\n    path: ../g-design/swift\n"
                   "targets:\n  GStudio:\n    sources:\n      - path: iOS/App\n",
    "Kit/Package.swift": 'let package = Package(name: "GStudioKit", dependencies: [\n'
                         '  .package(url: "https://github.com/x/SwiftTerm", exact: "1.0.0"),\n'
                         '  .package(path: "../../g-design/swift")\n])\n',
}


def test_studio_app_on_g_design_alone_passes(tmp_path, capsys):
    code, out = run(make(tmp_path / "g-studio-apple", APPLE), "g-studio-apple", capsys)
    assert code == 0, out
    assert "project.yml:5: ../g-design/swift -> g-design/swift" in out
    assert "Kit/Package.swift:3: ../../g-design/swift -> g-design/swift" in out
    assert "2 path(s) outside the repo, 0 not allowed" in out


def test_studio_reaching_into_command_or_another_studio_checkout_is_caught(tmp_path, capsys):
    files = dict(APPLE)
    files["project.yml"] += "      - path: ../g-studio/web/src\n"
    files["Kit/Package.swift"] = 'let p = Package(dependencies: [.package(name: "K", path: "../../g-command-apple/Kit")])\n'
    code, out = run(make(tmp_path / "g-studio-apple", files), "g-studio-apple", capsys)
    assert code == 1
    assert "-> g-studio/web/src: not allowed, g-studio-apple may use only the app-contract fixtures from g-studio" in out
    assert "-> g-command-apple/Kit: not allowed" in out


def test_studio_and_command_apps_may_read_the_shared_contract_fixtures(tmp_path, capsys):
    files = dict(APPLE)
    files["project.yml"] += "      - path: ../g-studio/tests/fixtures/app-contract\n"
    code, out = run(make(tmp_path / "g-studio-apple", files), "g-studio-apple", capsys)
    assert code == 0, out
    assert "-> g-studio/tests/fixtures/app-contract" in out
    code, out = run(make(tmp_path / "g-command-apple", files), "g-command-apple", capsys)
    assert code == 0, out


def test_command_apple_may_use_studio_kit_only(tmp_path, capsys):
    ok = {"project.yml": "packages:\n  GStudioKit:\n    path: ../g-studio-apple/Kit\n  GDesign:\n    path: ../g-design\n"}
    assert run(make(tmp_path / "ok" / "g-command-apple", ok), "g-command-apple", capsys)[0] == 0
    bad = {"project.yml": "targets:\n  GCommand:\n    sources:\n      - path: ../g-studio-apple/iOS/App/Views\n"}
    code, out = run(make(tmp_path / "bad" / "g-command-apple", bad), "g-command-apple", capsys)
    assert code == 1 and "may use only Kit/, the GStudioKit package from g-studio-apple" in out


def test_command_web_may_use_studio_mount_entry_not_its_source(tmp_path, capsys):
    ok = {"package.json": '{"dependencies": {"studio-mount": "file:../g-studio/web/dist/mount.js",'
                          ' "g-design": "file:../g-design", "preact": "10.0.0"}}\n'}
    code, out = run(make(tmp_path / "ok" / "g-command-web", ok), "g-command-web", capsys)
    assert code == 0, out
    bad = {"package.json": '{"devDependencies": {"studio": "link:../g-studio/web/src"}}\n'}
    code, out = run(make(tmp_path / "bad" / "g-command-web", bad), "g-command-web", capsys)
    assert code == 1 and "only Studio's published mount entry" in out


def test_worktree_sibling_takes_its_main_repo_name(tmp_path, capsys):
    main = make(tmp_path / "g-design", {"README.md": "x\n"})
    subprocess.run(["git", "-C", str(main), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"],
                   check=True)
    subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", str(tmp_path / "g-design-wt-kit")], check=True)
    files = {"project.yml": "packages:\n  GDesign:\n    path: ../g-design-wt-kit/swift\n"}
    code, out = run(make(tmp_path / "g-studio-apple", files), "g-studio-apple", capsys)
    assert code == 0 and "-> g-design/swift" in out, out


def test_paths_inside_the_repo_and_repos_without_a_rule(tmp_path, capsys):
    files = {"project.yml": "packages:\n  Kit:\n    path: Kit\n", "package.json": '{"dependencies": {"x": "file:./vendor/x"}}'}
    code, out = run(make(tmp_path / "g-studio", files), "g-studio", capsys)
    assert code == 0 and "0 path(s) outside the repo" in out
    files = {"package.json": '{"dependencies": {"s": "file:../g-studio/web"}}'}
    code, out = run(make(tmp_path / "gcore", files), "gcore", capsys)
    assert code == 0 and "-> g-studio/web" in out and "no sibling rule" in out
    assert siblings.main([str(tmp_path / "nowhere")]) == 2
