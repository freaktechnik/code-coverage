# -*- coding: utf-8 -*-
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import json
import os
import zipfile
from datetime import timedelta

import structlog

from code_coverage_bot import config
from code_coverage_bot import hgmo
from code_coverage_bot import uploader
from code_coverage_bot.cli import setup_cli
from code_coverage_bot.hooks.base import Hook
from code_coverage_bot.notifier import notify_email
from code_coverage_bot.phabricator import PhabricatorUploader
from code_coverage_bot.phabricator import parse_revision_id
from code_coverage_bot.secrets import secrets
from code_coverage_bot.taskcluster import taskcluster_config
from code_coverage_tools import gcp

logger = structlog.get_logger(__name__)


class RepositoryHook(Hook):
    """
    Base class to support specific workflows per repository
    """

    HOOK_NAME = "repo"

    def upload_reports(self, reports):
        """
        Upload all provided covdir reports on GCP
        """
        for (platform, suite), path in reports.items():
            report = open(path, "rb").read()
            uploader.gcp(
                self.branch, self.revision, report, suite=suite, platform=platform
            )

    def check_javascript_files(self):
        """
        Check that all JavaScript files present in the coverage artifacts actually exist.
        If they don't, there might be a bug in the LCOV rewriter.
        """
        for artifact in self.artifactsHandler.get():
            if "jsvm" not in artifact:
                continue

            with zipfile.ZipFile(artifact, "r") as zf:
                for file_name in zf.namelist():
                    with zf.open(file_name, "r") as fl:
                        source_files = [
                            line[3:].decode("utf-8").rstrip()
                            for line in fl
                            if line.startswith(b"SF:")
                        ]
                        missing_files = [
                            f
                            for f in source_files
                            if not os.path.exists(os.path.join(self.repo_dir, f))
                        ]
                        if len(missing_files) != 0:
                            logger.warn(
                                f"{missing_files} are present in coverage reports, but missing from the repository"
                            )

    def get_hgmo_changesets(self):
        """
        Build HGMO changesets according to this repo's configuration
        """
        with hgmo.HGMO(server_address=self.repository) as hgmo_server:
            return hgmo_server.get_automation_relevance_changesets(self.revision)

    def upload_phabricator(self, report, changesets):
        """
        Helper to upload coverage report on Phabricator
        """
        phabricatorUploader = PhabricatorUploader(self.repo_dir, self.revision)
        logger.info("Upload changeset coverage data to Phabricator")
        return phabricatorUploader.upload(report, changesets)


class MozillaCentralHook(RepositoryHook):
    """
    Code coverage hook for mozilla-central
    * Check coverage artifacts content
    * Build all covdir reports possible
    * Upload all reports on GCP
    * Upload main reports on Phabrictaor
    * Send an email to admins on low coverage
    """

    def __init__(self, *args, **kwargs):
        super().__init__(
            # On mozilla-central, we want to assert that every platform was run (except for android platforms
            # as they are unstable).
            required_platforms=["linux", "windows"],
            *args,
            **kwargs,
        )

    def run(self):
        # Check the covdir report does not already exists
        bucket = gcp.get_bucket(secrets[secrets.GOOGLE_CLOUD_STORAGE])
        if uploader.gcp_covdir_exists(bucket, self.branch, self.revision, "all", "all"):
            logger.warn("Full covdir report already on GCP")
            return

        # Generate and upload the full report as soon as possible, so it is available
        # for consumers (e.g. Searchfox) right away.
        self.retrieve_source_and_artifacts()

        reports = self.build_reports(only=[("all", "all")])

        full_path = reports.get(("all", "all"))
        assert full_path is not None, "Missing full report (all:all)"
        with open(full_path, "r") as f:
            report_text = f.read()

        # Upload report as an artifact.
        taskcluster_config.upload_artifact(
            "public/code-coverage-report.json",
            report_text,
            "application/json",
            timedelta(days=14),
        )

        # Index on Taskcluster
        self.index_task(
            [
                "{}.{}".format(self.hook, self.revision),
                "{}.latest".format(self.hook),
            ]
        )

        report = json.loads(report_text)

        # Check extensions
        paths = uploader.covdir_paths(report)
        for extension in [".js", ".cpp"]:
            assert any(
                path.endswith(extension) for path in paths
            ), "No {} file in the generated report".format(extension)

        # Upload coverage on phabricator
        changesets = self.get_hgmo_changesets()
        coverage = self.upload_phabricator(report, changesets)

        # Send an email on low coverage
        notify_email(self.revision, changesets, coverage)
        logger.info("Sent low coverage email notification")

        if secrets.get(secrets.CHECK_JAVASCRIPT_FILES, False):
            self.check_javascript_files()

        # Generate all reports except the full one which we generated earlier.
        all_report_combinations = self.artifactsHandler.get_combinations()
        del all_report_combinations[("all", "all")]
        reports.update(self.build_reports())
        logger.info("Built all covdir reports", nb=len(reports))

        # Upload reports on GCP
        self.upload_reports(reports)
        logger.info("Uploaded all covdir reports", nb=len(reports))


class TryHook(RepositoryHook):
    """
    Code coverage hook for a try push
    * Build only main covdir report
    * Upload that report on Phabrictaor
    """

    def __init__(self, *args, **kwargs):
        super().__init__(
            # On try, developers might have requested to run only one platform, and we trust them.
            required_platforms=[],
            *args,
            **kwargs,
        )

    def run(self):
        changesets = self.get_hgmo_changesets()

        if not any(
            parse_revision_id(changeset["desc"]) is not None for changeset in changesets
        ):
            logger.info(
                "None of the commits in the try push are linked to a Phabricator revision"
            )
            return

        self.retrieve_source_and_artifacts()

        reports = self.build_reports(only=[("all", "all")])
        logger.info("Built all covdir reports", nb=len(reports))

        # Retrieve the full report
        full_path = reports.get(("all", "all"))
        assert full_path is not None, "Missing full report (all:all)"
        report = json.load(open(full_path))

        # Upload coverage on phabricator
        self.upload_phabricator(report, changesets)

        # Index on Taskcluster
        self.index_task(
            [
                "{}.{}".format(self.hook_path, self.revision),
                "project.relman.code-coverage.{}.repo.{}.latest".format(
                    secrets[secrets.APP_CHANNEL], self.project
                ),
            ]
        )


def main():
    logger.info("Starting code coverage bot for repository")
    args = setup_cli()

    namespace = args.namespace or config.DEFAULT_NAMESPACE
    project = args.project or config.DEFAULT_PROJECT
    repository = args.repository or config.DEFAULT_REPOSITORY
    upstream = args.upstream or config.DEFAULT_UPSTREAM

    hooks = {
        "central": MozillaCentralHook,
        "try": TryHook,
    }
    hook_class = hooks.get(args.hook)
    assert hook_class is not None, f"Unsupported hook type {args.hook}"

    hook = hook_class(
        namespace,
        project,
        repository,
        upstream,
        args.revision,
        args.task_name_filter,
        args.cache_root,
        args.working_dir,
    )
    hook.run()
