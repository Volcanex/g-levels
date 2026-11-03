import subprocess

from glevels import oneway

CLEAN = {
    "Kit/Package.swift": 'let package = Package(name: "GStudioKit",\n'
                         '  dependencies: [.package(url: "https://github.com/x/SwiftTerm", exact: "1.0.0")])\n',
    "project.yml": "packages:\n  GStudioKit:\n    path: Kit\n  GDesign:\n    path: ../g-design/swift\n"
                   "targets:\n  GStudio:\n    sources:\n      - path: iOS/App\n"
                   "    dependencies:\n      - package: GStudioKit\n",
    "iOS/App/Home.swift": "import SwiftUI\n@testable import GStudioCore\n// Copied from GCommandCore's Session.\n",
    "web/package.json": '{"dependencies": {"preact": "10.0.0", "g-design": "file:../../g-design"}}\n',
    "web/src/main.js": 'import { h } from "preact";\nimport "./style.css";\n'
                       'const admin = "https://system.gabrielpenman.com"; // gcommand reads /api/gadmin\n'
                       'document.querySelector(\'meta[name="g-command"]\');\n',
    "gstudio/cors.py": 'import os\nORIGINS = ["https://system.gabrielpenman.com", "https://command.gabrielpenman.com"]\n'
                       '# gcommand calls /api/gadmin/* on the raw port\n',
    "README.md": "Studio is not G-Command. Never import g-command-web.\n",
}


def make(root, files):
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    return root


def run(repo, name, capsys):
    code = oneway.main([str(repo), "--as", name])
    return code, capsys.readouterr().out


def test_clean_studio_repo_passes(tmp_path, capsys):
    code, out = run(make(tmp_path / "g-studio-apple", CLEAN), "g-studio-apple", capsys)
    assert code == 0, out
    assert "no reference to G-Command" in out


def test_planted_command_references_are_caught_with_file_and_line(tmp_path, capsys):
    planted = dict(CLEAN)
    planted["Kit/Package.swift"] += 'let more = [.package(path: "../../g-command-apple/Kit")]\n'
    planted["iOS/App/Chat.swift"] = "import SwiftUI\nimport GCommandUI\n"
    planted["project.yml"] += "      - package: GCommandKit\n"
    planted["web/package.json"] = '{"dependencies": {"@g-command/panels": "file:../../g-command-web/panels"}}\n'
    planted["web/src/panel.mjs"] = ('import {\n  Panel,\n} from "../../../g-command-web/core/static/panel.js";\n'
                                    'const t = await import("gcommand-tickets");\nconst x = require(\'g-command-shared\');\n')
    planted["gstudio/bridge.py"] = "import json, gcommand.core as gc\nfrom gcommand.api import routes\n"
    code, out = run(make(tmp_path / "g-studio", planted), "g-studio", capsys)
    assert code == 1
    for hit in ("Kit/Package.swift:3:", "iOS/App/Chat.swift:2: Swift import", "project.yml:12:",
                "web/package.json:1:", "web/src/panel.mjs:3:", "web/src/panel.mjs:4:", "web/src/panel.mjs:5:",
                "gstudio/bridge.py:1:", "gstudio/bridge.py:2:"):
        assert hit in out, (hit, out)
    assert "main.js" not in out and "cors.py" not in out and "README" not in out and "Home.swift" not in out


def test_g_design_may_reference_neither_studio_nor_command(tmp_path, capsys):
    files = {"package.json": '{"dependencies": {"@radix-ui/colors": "^3.0.0"}}\n',
             "scripts/vendor.mjs": "// e.g. node scripts/vendor.mjs ../g-command-web/core/static/css\nimport fs from 'node:fs';\n",
             "packages/g-ui/src/admin/g-admin.js": 'const COMMAND = meta("g-command");\n'}
    repo = make(tmp_path / "g-design", files)
    assert run(repo, "g-design", capsys)[0] == 0
    (repo / "packages/g-ui/src/studio.js").write_text('export * from "../../../../g-studio/web/src/tokens.js";\n')
    (repo / "server.py").write_text("from gstudio import tokens\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    code, out = run(repo, "g-design", capsys)
    assert code == 1
    assert "packages/g-ui/src/studio.js:1: import names Studio or G-Command" in out
    assert "server.py:1:" in out


def test_command_and_unknown_repos_have_no_rule(tmp_path, capsys):
    repo = make(tmp_path / "g-command-web", {"x.py": "import gstudio\n"})
    code, out = oneway.main([str(repo)]), capsys.readouterr().out
    assert code == 0 and "no one-way rule" in out


def test_untracked_repo_is_walked_and_missing_repo_cannot_run(tmp_path, capsys):
    plain = tmp_path / "plain"
    (plain / "node_modules/g-command").mkdir(parents=True)
    (plain / "node_modules/g-command/index.js").write_text('import "g-command";\n')
    (plain / "a.swift").write_text("import GCommandCore\n")
    code, out = run(plain, "g-studio-apple", capsys)
    assert code == 1 and "a.swift:1:" in out and "node_modules" not in out
    assert oneway.main([str(tmp_path / "nowhere")]) == 2
