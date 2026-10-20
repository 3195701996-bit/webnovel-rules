"""Release APK verifier must fail closed before a candidate can be distributed."""

import os
import subprocess
import zipfile
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
VERIFIER = REPO / "tools" / "verify_android_release.sh"


def _executable(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)
    return path


def _fixture(tmp_path, *, candidate_package="com.webnovel.mobile",
             candidate_code="200", candidate_name="2.0.0",
             candidate_cert="a" * 64, signature="v2",
             candidate_revision="b" * 40, candidate_dirty="clean",
             identity_name=None, identity_code=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    previous, candidate = (tmp_path / name for name in ("previous.apk", "candidate.apk"))
    for apk in (previous, candidate):
        with zipfile.ZipFile(apk, "w") as archive:
            archive.writestr("AndroidManifest.xml", b"fixture")
            if apk == candidate:
                archive.writestr("assets/build-identity.properties", (
                    f"revision={candidate_revision}\n"
                    f"dirty={candidate_dirty}\n"
                    f"versionName={identity_name or candidate_name}\n"
                    f"versionCode={identity_code or candidate_code}\n"
                ))

    tool_dir = tmp_path / "android tools"
    tool_dir.mkdir()
    aapt = _executable(tool_dir / "aapt", """#!/bin/sh
case "$(basename "$3")" in
  previous.apk) echo "package: name='com.webnovel.mobile' versionCode='154' versionName='1.6.4'" ;;
  candidate.apk) echo "package: name='$MOCK_PACKAGE' versionCode='$MOCK_CODE' versionName='$MOCK_NAME'" ;;
  *) exit 2 ;;
esac
""")
    apksigner = _executable(tool_dir / "apksigner", """#!/bin/sh
if [ "$1" != verify ]; then exit 2; fi
case "$(basename "$4")" in
  previous.apk) cert="$MOCK_PREVIOUS_CERT" ;;
  candidate.apk) cert="$MOCK_CANDIDATE_CERT" ;;
  *) exit 2 ;;
esac
if [ "$MOCK_SIGNATURE" = v2 ]; then
  echo 'Verified using v2 scheme (APK Signature Scheme v2): true'
elif [ "$MOCK_SIGNATURE" = v3 ]; then
  echo 'Verified using v3 scheme (APK Signature Scheme v3): true'
fi
echo "Signer #1 certificate SHA-256 digest: $cert"
""")

    env = os.environ.copy()
    env.update({
        "AAPT": str(aapt),
        "APKSIGNER": str(apksigner),
        "MOCK_PACKAGE": candidate_package,
        "MOCK_CODE": candidate_code,
        "MOCK_NAME": candidate_name,
        "MOCK_PREVIOUS_CERT": "a" * 64,
        "MOCK_CANDIDATE_CERT": candidate_cert,
        "MOCK_SIGNATURE": signature,
    })
    return previous, candidate, env


def _run(paths, env, *, expected_code="200", expected_revision="b" * 40):
    previous, candidate, _ = paths
    env = env.copy()
    env["EXPECTED_GIT_REVISION"] = expected_revision
    return subprocess.run(
        [str(VERIFIER), str(previous), str(candidate), "2.0.0", expected_code],
        cwd=REPO, env=env, text=True, capture_output=True, check=False,
    )


def test_accepts_higher_version_with_matching_package_and_signer(tmp_path):
    previous, candidate, env = _fixture(tmp_path)
    result = _run((previous, candidate, env), env)

    assert result.returncode == 0, result.stderr
    assert "APK release 验收通过" in result.stdout
    assert "候选版: 2.0.0 / code 200" in result.stdout
    assert "APK SHA-256:" in result.stdout


def test_accepts_valid_v3_only_signature(tmp_path):
    previous, candidate, env = _fixture(tmp_path, signature="v3")
    result = _run((previous, candidate, env), env)

    assert result.returncode == 0, result.stderr
    assert "APK release 验收通过" in result.stdout


def test_requires_exact_expected_git_revision(tmp_path):
    previous, candidate, env = _fixture(tmp_path)
    missing = env.copy()
    missing.pop("EXPECTED_GIT_REVISION", None)
    no_revision = subprocess.run(
        [str(VERIFIER), str(previous), str(candidate), "2.0.0", "200"],
        cwd=REPO, env=missing, text=True, capture_output=True, check=False,
    )
    assert no_revision.returncode == 2
    assert "必须通过 EXPECTED_GIT_REVISION" in no_revision.stderr

    malformed = _run((previous, candidate, env), env, expected_revision="b" * 39)
    assert malformed.returncode == 2
    assert "完整 40 位 Git SHA" in malformed.stderr

    mismatch = _run((previous, candidate, env), env, expected_revision="c" * 40)
    assert mismatch.returncode == 1
    assert "revision 与指定 revision 不一致" in mismatch.stderr


def test_rejects_wrong_application_id(tmp_path):
    previous, candidate, env = _fixture(tmp_path, candidate_package="com.other.app")
    result = _run((previous, candidate, env), env)

    assert result.returncode == 1
    assert "应用 ID 不一致" in result.stderr


def test_rejects_non_increasing_version_code(tmp_path):
    previous, candidate, env = _fixture(tmp_path, candidate_code="154")
    result = _run((previous, candidate, env), env, expected_code="154")

    assert result.returncode == 1
    assert "必须高于上一版" in result.stderr


def test_rejects_signer_mismatch_and_missing_v2_or_v3_signature(tmp_path):
    previous, candidate, env = _fixture(tmp_path, candidate_cert="b" * 64)
    result = _run((previous, candidate, env), env)
    assert result.returncode == 1
    assert "签名证书与上一版不一致" in result.stderr

    previous2, candidate2, env2 = _fixture(
        tmp_path / "unsigned", signature="none")
    result2 = _run((previous2, candidate2, env2), env2)
    assert result2.returncode == 1
    assert "缺少有效的 v2/v3 签名" in result2.stderr


def test_rejects_dirty_unknown_or_wrong_revision_build_identity(tmp_path):
    previous, candidate, env = _fixture(
        tmp_path / "dirty", candidate_dirty="3 files")
    result = _run((previous, candidate, env), env)
    assert result.returncode == 1
    assert "源码状态为 clean" in result.stderr

    previous2, candidate2, env2 = _fixture(
        tmp_path / "unknown", candidate_revision="unknown")
    result2 = _run((previous2, candidate2, env2), env2)
    assert result2.returncode == 1
    assert "完整 Git SHA" in result2.stderr

    previous3, candidate3, env3 = _fixture(
        tmp_path / "wrong-version", identity_code="199")
    result3 = _run((previous3, candidate3, env3), env3)
    assert result3.returncode == 1
    assert "版本不一致" in result3.stderr
