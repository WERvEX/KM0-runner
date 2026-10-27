"""Finding Git for Windows' bash, laid out in a temporary folder so it runs on every platform."""

from km0_runner.pi_client import windows_bash


def touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return str(path)


def which_from(found):
    return lambda name: found.get(name)


def test_the_bash_on_path_wins_unless_it_is_wsls(tmp_path):
    msys = touch(tmp_path / "msys64" / "usr" / "bin" / "bash.exe")
    git = touch(tmp_path / "Git" / "cmd" / "git.exe")
    touch(tmp_path / "Git" / "bin" / "bash.exe")
    environ = {"SystemRoot": str(tmp_path / "Windows")}

    assert windows_bash(which_from({"bash": msys, "git": git}), environ) == msys


def test_wsls_bash_gives_way_to_the_one_next_to_git(tmp_path):
    wsl = touch(tmp_path / "Windows" / "System32" / "bash.exe")
    git = touch(tmp_path / "Git" / "cmd" / "git.exe")
    git_bash = touch(tmp_path / "Git" / "bin" / "bash.exe")
    environ = {"SystemRoot": str(tmp_path / "Windows")}

    assert windows_bash(which_from({"bash": wsl, "git": git}), environ) == git_bash
    # Git's own bin and mingw64 layouts lead to the same install.
    for layout in (("bin", "git.exe"), ("mingw64", "bin", "git.exe")):
        other = touch(tmp_path.joinpath("Git", *layout))
        assert windows_bash(which_from({"bash": wsl, "git": other}), environ) == git_bash


def test_no_bash_on_path_falls_back_to_program_files(tmp_path):
    git_bash = touch(tmp_path / "Program Files" / "Git" / "bin" / "bash.exe")
    environ = {"SystemRoot": str(tmp_path / "Windows"), "ProgramFiles": str(tmp_path / "Program Files")}

    assert windows_bash(which_from({}), environ) == git_bash


def test_nothing_better_keeps_what_path_found(tmp_path):
    wsl = touch(tmp_path / "Windows" / "System32" / "bash.exe")
    environ = {"SystemRoot": str(tmp_path / "Windows")}

    assert windows_bash(which_from({"bash": wsl}), environ) == wsl
    assert windows_bash(which_from({}), environ) is None
