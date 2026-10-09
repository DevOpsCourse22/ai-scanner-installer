"""The installer's own copies of the three Jenkinsfiles.

The code repositories stay untouched: at install time the installer reads
each repository's Jenkinsfile and ci/pod.yaml from GitHub and derives a
variant that

  - checks the code repository out itself (the Jenkins job's own SCM is
    this installer repository, which holds the Jenkinsfile),
  - builds main only (the original builds every branch through a
    multibranch job; this job follows main),
  - deploys by committing the new image tag to THIS repository, on the
    environment's branch (env/<env>), in environments/<env>/<app>/app-patch.yaml,
    instead of to the shared *-deployment repositories.

Everything else (Test, Build, Scan, Push, the registry switch) is the
original pipeline, unchanged. The derived files are committed next to the
manifests, so the pipeline that runs is always readable in git.
"""

from __future__ import annotations

import re

APPS = {"ai-scanner-backend": "backend", "ai-scanner-gateway": "gateway", "ai-scanner-frontend": "frontend"}

# Shell functions that replace ci/deploy-lib.sh's clone/commit for this layout.
# Sourced after the original library, so the original set_image stays unused.
OVERRIDES = r'''
                            # --- ai-scanner-installer: deploy to this install's own branch ---
                            clone_deploy_repo() {
                                ASKPASS="$(mktemp)"
                                printf '#!/bin/sh\necho "$GIT_PASS"\n' > "$ASKPASS"
                                chmod 700 "$ASKPASS"
                                export GIT_ASKPASS="$ASKPASS" GIT_TERMINAL_PROMPT=0
                                rm -rf deploy
                                git clone --quiet --depth 1 --branch "$DEPLOY_BRANCH" "https://${GIT_USER}@github.com/${DEPLOY_REPO}.git" deploy
                                cd deploy
                            }
                            set_env_image() {   # <image> <app>
                                f="environments/${DEPLOY_ENV}/$2/app-patch.yaml"
                                sed -i "s|image: [^ ]*/$1:[^ ]*|image: ${REGISTRY}/$1:${TAG}|" "$f"
                                grep -q "image: ${REGISTRY}/$1:${TAG}" "$f" || { echo "no image line for $1 in $f" >&2; exit 1; }
                            }
                            commit_and_push() {
                                if git diff --quiet; then echo "Already at ${TAG}; nothing to commit."; return 0; fi
                                git -c user.name="Jenkins CI" -c user.email="jenkins@ai-scan-repos.local" commit --quiet -am "ci(deploy): $1 -> ${TAG}

Built by Jenkins (${JOB_NAME} ${BUILD_URL:-}) from ${CODE_OWNER}/${CODE_REPO}@${GIT_COMMIT}."
                                for attempt in 1 2 3; do
                                    if git push --quiet origin "HEAD:${DEPLOY_BRANCH}"; then echo "Pushed: $1 -> ${TAG}"; return 0; fi
                                    git pull --quiet --rebase origin "$DEPLOY_BRANCH"
                                done
                                echo "Could not push to ${DEPLOY_REPO} ${DEPLOY_BRANCH}" >&2; return 1
                            }
'''


class PipelineError(ValueError):
    """The original Jenkinsfile no longer has the shape this derivation expects."""


def _sub(pattern: str, repl: str, text: str, what: str, count: int = 0, flags: int = 0) -> str:
    new, n = re.subn(pattern, repl, text, count=count, flags=flags)
    if n == 0:
        raise PipelineError(f"could not find {what} in the Jenkinsfile")
    return new


def derive(repo: str, original: str, env: str) -> str:
    """The installer variant of one code repository's Jenkinsfile."""
    app = APPS[repo]
    text = original
    text = _sub(r"yamlFile 'ci/pod\.yaml'", f"yamlFile 'environments/{env}/ci/{repo}.pod.yaml'", text, "yamlFile")
    text = _sub(r"options \{", "options {\n        skipDefaultCheckout()               // the code is checked out in the Checkout stage",
                text, "options", count=1)
    text = _sub(r"\n\s*TAG\s*=\s*\"\$\{env\.GIT_COMMIT\.take\(7\)\}\"[^\n]*", "", text, "TAG")
    text = _sub(r"DEPLOY_REPO\s*=\s*[^\n]*",
                "DEPLOY_REPO   = \"${env.INSTALLER_REPO}\"      // this installer repository, branch DEPLOY_BRANCH\n"
                f"        CODE_REPO     = '{repo}'", text, "DEPLOY_REPO")
    checkout = f"""stages {{
        stage('Checkout') {{
            steps {{
                script {{
                    def scmVars = checkout scmGit(
                        userRemoteConfigs: [[url: "https://github.com/${{env.CODE_OWNER}}/{repo}.git",
                                             credentialsId: 'github-deploy']],
                        branches: [[name: '*/main']])
                    env.GIT_COMMIT = scmVars.GIT_COMMIT
                    env.TAG = scmVars.GIT_COMMIT.take(7)    // the image tag is the commit it was built from
                }}
            }}
        }}
"""
    text = _sub(r"stages \{\n", checkout, text, "stages", count=1)
    text = _sub(r"when \{ branch 'main' \}", "when { expression { true } }   // this job builds main only", text, "Push condition")
    text = _sub(r"allOf \{ branch 'main'; ", "allOf { ", text, "Deploy condition")
    text = _sub(r"(\n\s*)\. ci/deploy-lib\.sh", r"\1. ci/deploy-lib.sh" + OVERRIDES.replace("\\", "\\\\"), text, "deploy-lib")
    text = _sub(r"set_image (\S+) kustomization\.yaml", rf"set_env_image \1 {app}", text, "set_image")
    if app == "backend":
        text = _sub(r'review-api/deployment\.yaml', f"environments/${{DEPLOY_ENV}}/{app}/app-patch.yaml", text,
                    "scanner-worker line")
    header = (f"// Derived by ai-scanner-installer from {repo}/Jenkinsfile (main) for the '{env}' environment.\n"
              "// Do not edit here: the installer writes it again on every install or upgrade.\n\n")
    return header + text
