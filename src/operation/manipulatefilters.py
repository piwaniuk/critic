# -*- mode: python; encoding: utf-8 -*-
#
# Copyright 2012 Jens Lindström, Opera Software ASA
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License.  You may obtain a copy of
# the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.  See the
# License for the specific language governing permissions and limitations under
# the License.

import dbutils
import gitutils
import mailutils
import htmlutils
import reviewing.utils
import reviewing.filters

from operation import Operation, OperationResult, OperationError, \
    OperationFailure, OperationFailureMustLogin, Optional

class AddFilter(Operation):
    def __init__(self):
        Operation.__init__(self, { "filter_type": set(["reviewer", "watcher", "ignored"]),
                                   "path": str,
                                   "delegates": [str],
                                   "repository_id": Optional(int),
                                   "repository_name": Optional(str),
                                   "replaced_filter_id": Optional(int) })

    def process(self, db, user, filter_type, path, delegates, repository_id=None, repository_name=None, replaced_filter_id=None):
        path = reviewing.filters.sanitizePath(path)

        if "*" in path:
            try:
                reviewing.filters.validatePattern(path)
            except reviewing.filters.PatternError as error:
                raise OperationFailure(code="invalidpattern",
                                       title="Invalid path pattern",
                                       message="There are invalid wild-cards in the path: %s" % error.message)

        if filter_type == "reviewer":
            delegates = filter(None, delegates)
            invalid_delegates = []
            for delegate in delegates:
                try:
                    dbutils.User.fromName(db, delegate)
                except dbutils.NoSuchUser:
                    invalid_delegates.append(delegate)
            if invalid_delegates:
                raise OperationFailure(code="invaliduser",
                                       title="Invalid delegate(s)",
                                       message="These user-names are not valid: %s" % ", ".join(invalid_delegates))
        else:
            delegates = []

        cursor = db.cursor()

        if repository_id is None:
            cursor.execute("""SELECT id
                                FROM repositories
                               WHERE name=%s""",
                           (repository_name,))
            repository_id = cursor.fetchone()[0]

        if replaced_filter_id is not None:
            cursor.execute("""SELECT 1
                                FROM filters
                               WHERE id=%s
                                 AND uid=%s""",
                           (replaced_filter_id, user.id))

            if not cursor.fetchone():
                raise OperationFailure(code="invalidoperation",
                                       title="Invalid operation",
                                       message="Filter to replace does not exist or belongs to another user!")

            cursor.execute("""DELETE
                                FROM filters
                               WHERE id=%s""",
                           (replaced_filter_id,))

        cursor.execute("""SELECT 1
                            FROM filters
                           WHERE uid=%s
                             AND repository=%s
                             AND path=%s""",
                       (user.id, repository_id, path))

        if cursor.fetchone():
            raise OperationFailure(code="duplicatefilter",
                                   title="Duplicate filter",
                                   message=("You already have a filter for the path <code>%s</code> in this repository."
                                            % htmlutils.htmlify(path)),
                                   is_html=True)

        cursor.execute("""INSERT INTO filters (uid, repository, path, type, delegate)
                               VALUES (%s, %s, %s, %s, %s)
                            RETURNING id""",
                       (user.id, repository_id, path, filter_type, ",".join(delegates)))

        filter_id = cursor.fetchone()[0]

        db.commit()

        return OperationResult(filter_id=filter_id)

class DeleteFilter(Operation):
    def __init__(self):
        Operation.__init__(self, { "filter_id": int })

    def process(self, db, user, filter_id):
        cursor = db.cursor()
        cursor.execute("""SELECT uid
                            FROM filters
                           WHERE id=%s""",
                       (filter_id,))

        row = cursor.fetchone()
        if row:
            if user.id != row[0]:
                Operation.requireAdministratorRole(db, user)

            cursor.execute("""DELETE
                                FROM filters
                               WHERE id=%s""",
                           (filter_id,))

            db.commit()

        return OperationResult()

class ReapplyReviewFilters(Operation):
    def __init__(self):
        Operation.__init__(self, {"review_id": int, "dry_run": Optional(bool)})

    def process(self, db, user, review_id, dry_run=False):
        if user.isAnonymous():
            return OperationFailureMustLogin()

        review = dbutils.review.Review.fromId(db, review_id)

        if not user.hasRole(db, "administrator") and user.id not in (owner.id for owner in review.owners):
            raise OperationFailure(
                code="notallowed",
                title="Not allowed!",
                message="Operation not permitted.")

        reviewers, watchers = reviewing.utils.getReviewersAndWatchers(
            db,
            review.repository,
            commits=review.getCommitSet(db),
            reviewfilters=review.getReviewFilters(db),
            applyfilters=review.applyfilters,
            applyparentfilters=review.applyparentfilters,
            reviewerbestmatch=True)

        new_review_users = set()
        new_review_user_files = set()

        for file_id, file_users in reviewers.items():
            for user_id, user_changesets in file_users.items():
                if user_id:
                    new_review_users.add(user_id)

                    for changeset_id in user_changesets:
                        new_review_user_files.add((user_id, changeset_id,
                                                  file_id))

        for file_id, file_users in watchers.items():
            for user_id, _ in file_users.items():
                if user_id:
                    new_review_users.add(user_id)

        old_review_users = review.getReviewUsers(db)
        old_review_user_files = review.getReviewUserFiles(db)

        review_user_files_remove = old_review_user_files - new_review_user_files
        review_user_files_add = new_review_user_files - old_review_user_files

        review_users_remove = old_review_users - new_review_users - set(
            owner.id for owner in review.owners)
        review_users_add = new_review_users - old_review_users

        if not dry_run:
            cursor = db.cursor()
            cursor.executemany(
                "DELETE FROM reviewusers WHERE review=%s AND uid=%s",
                ((review.id, user) for user in review_users_remove))
            cursor.executemany(
                "INSERT INTO reviewusers (review, uid) VALUES (%s, %s)",
                ((review.id, user) for user in review_users_add))

            cursor.executemany(
                "DELETE FROM reviewuserfiles WHERE uid=%s and file=(SELECT id FROM reviewfiles WHERE review=%s AND changeset=%s AND file=%s)",
                ((user_id, review.id, changeset_id, file_id)
                 for (user_id, changeset_id, file_id) in review_user_files_remove))
            cursor.executemany(
                "INSERT INTO reviewuserfiles (file, uid) SELECT id, %s FROM reviewfiles WHERE review=%s AND changeset=%s AND file=%s",
                ((user_id, review.id, changeset_id, file_id)
                 for (user_id, changeset_id, file_id) in review_user_files_add))
            db.commit()

        return OperationResult(
            added_users=list(review_users_add),
            removed_users=list(review_users_remove),
            added_filters=list(review_user_files_add),
            removed_filters=list(review_user_files_remove))


class ReapplyFilters(Operation):
    def __init__(self):
        Operation.__init__(self, { "repository_id": Optional(int),
                                   "filter_id": Optional(int) })

    def process(self, db, user, repository_id=None, filter_id=None):
        if user.isAnonymous():
            return OperationFailureMustLogin()

        cursor = db.cursor()

        if filter_id is not None:
            cursor.execute("""SELECT repository, path, type, delegate
                                FROM filters
                               WHERE id=%s""",
                           (filter_id,))
            repository_id, filter_path, filter_type, filter_delegate = cursor.fetchone()

        if repository_id is None:
            cursor.execute("""SELECT reviews.id, applyfilters, applyparentfilters, branches.repository
                                FROM reviews
                                JOIN branches ON (reviews.branch=branches.id)
                               WHERE reviews.state!='closed'""")
        else:
            cursor.execute("""SELECT reviews.id, applyfilters, applyparentfilters, branches.repository
                                FROM reviews
                                JOIN branches ON (reviews.branch=branches.id)
                               WHERE reviews.state!='closed'
                                 AND branches.repository=%s""",
                           (repository_id,))

        repositories = {}

        # list(review_file_id)
        assign_changes = []

        # set(review_id)
        assigned_reviews = set()

        # set(review_id)
        watched_reviews = set()

        for review_id, applyfilters, applyparentfilters, repository_id in cursor.fetchall():
            if repository_id in repositories:
                repository = repositories[repository_id]
            else:
                repository = gitutils.Repository.fromId(db, repository_id)
                repositories[repository_id] = repository

            review = reviewing.filters.Filters.Review(review_id, applyfilters, applyparentfilters, repository)
            filters = reviewing.filters.Filters()

            filters.setFiles(db, review=review)

            if filter_id is not None:
                filters.addFilter(user.id, filter_path, filter_type, filter_delegate, filter_id)
            else:
                filters.load(db, review=review, user=user)

            cursor.execute("""SELECT commits.id, reviewfiles.file, reviewfiles.id
                                FROM commits
                                JOIN gitusers ON (gitusers.id=commits.author_gituser)
                     LEFT OUTER JOIN usergitemails ON (usergitemails.email=gitusers.email
                                                   AND usergitemails.uid=%s)
                                JOIN changesets ON (changesets.child=commits.id)
                                JOIN reviewfiles ON (reviewfiles.changeset=changesets.id)
                     LEFT OUTER JOIN reviewuserfiles ON (reviewuserfiles.file=reviewfiles.id
                                                     AND reviewuserfiles.uid=%s)
                               WHERE reviewfiles.review=%s
                                 AND usergitemails.uid IS NULL
                                 AND reviewuserfiles.uid IS NULL""",
                            (user.id, user.id, review_id))

            for commit_id, file_id, review_file_id in cursor.fetchall():
                association = filters.getUserFileAssociation(user.id, file_id)

                if association == 'reviewer':
                    assign_changes.append(review_file_id)
                    assigned_reviews.add(review_id)
                elif association == 'watcher':
                    watched_reviews.add(review_id)

        cursor.execute("""SELECT reviews.id
                            FROM reviews
                 LEFT OUTER JOIN reviewusers ON (reviewusers.review=reviews.id
                                             AND reviewusers.uid=%s)
                           WHERE reviews.id=ANY (%s)
                             AND reviewusers.uid IS NULL""",
                       (user.id, list(assigned_reviews) + list(watched_reviews)))

        new_reviews = set(review_id for (review_id,) in cursor)

        cursor.executemany("""INSERT INTO reviewusers (review, uid)
                                   VALUES (%s, %s)""",
                           [(review_id, user.id) for review_id in new_reviews])

        cursor.executemany("""INSERT INTO reviewuserfiles (file, uid)
                                   VALUES (%s, %s)""",
                           [(review_file_id, user.id) for review_file_id in assign_changes])

        db.commit()

        watched_reviews &= new_reviews
        watched_reviews -= assigned_reviews

        cursor.execute("""SELECT id, summary
                            FROM reviews
                           WHERE id=ANY (%s)""",
                       (list(assigned_reviews | watched_reviews),))

        return OperationResult(assigned_reviews=sorted(assigned_reviews),
                               watched_reviews=sorted(watched_reviews),
                               summaries=dict(cursor))

class CountMatchedPaths(Operation):
    def __init__(self):
        Operation.__init__(self, { "single": Optional({ "repository_name": str,
                                                        "path": str }),
                                   "multiple": Optional([int]),
                                   "user_id": Optional(int) })

    def process(self, db, user, single=None, multiple=None, user_id=None):
        if user_id is None:
            user_id = user.id

        try:
            if single:
                repository = gitutils.Repository.fromName(db, single["repository_name"])
                path = reviewing.filters.sanitizePath(single["path"])

                cursor = db.cursor()
                cursor.execute("""SELECT path
                                    FROM filters
                                   WHERE repository=%s
                                     AND uid=%s""",
                               (repository.id, user_id,))

                paths = set(filter_path for (filter_path,) in cursor)
                paths.add(path)

                return OperationResult(count=reviewing.filters.countMatchedFiles(repository, list(paths))[path])

            cursor = db.cursor()
            cursor.execute("""SELECT repository, id, path
                                FROM filters
                               WHERE id=ANY (%s)
                            ORDER BY repository""",
                           (multiple,))

            per_repository = {}
            result = []

            for repository_id, filter_id, filter_path in cursor:
                per_repository.setdefault(repository_id, []).append((filter_id, filter_path))

            for repository_id, filters in per_repository.items():
                repository = gitutils.Repository.fromId(db, repository_id)
                counts = reviewing.filters.countMatchedFiles(
                    repository, [filter_path for (filter_id, filter_path) in filters])
                for filter_id, filter_path in filters:
                    result.append({ "id": filter_id,
                                    "count": counts[filter_path] })

            return OperationResult(filters=result)
        except reviewing.filters.PatternError as error:
            return OperationFailure(code="invalidpattern",
                                    title="Invalid pattern!",
                                    message=str(error))

class GetMatchedPaths(Operation):
    def __init__(self):
        Operation.__init__(self, { "repository_name": str,
                                   "path": str,
                                   "user_id": Optional(int) })

    def process(self, db, user, repository_name, path, user_id=None):
        if user_id is None:
            user_id = user.id

        repository = gitutils.Repository.fromName(db, repository_name)
        path = reviewing.filters.sanitizePath(path)

        cursor = db.cursor()
        cursor.execute("""SELECT path
                            FROM filters
                           WHERE repository=%s
                             AND uid=%s""",
                       (repository.id, user_id,))

        paths = set(filter_path for (filter_path,) in cursor)
        paths.add(path)

        return OperationResult(paths=reviewing.filters.getMatchedFiles(repository, list(paths))[path])

class AddReviewFilters(Operation):
    def __init__(self):
        Operation.__init__(self, { "review_id": int,
                                   "filters": [{ "type": set(["reviewer", "watcher"]),
                                                 "user_names": Optional([str]),
                                                 "user_ids": Optional([int]),
                                                 "paths": Optional([str]),
                                                 "file_ids": Optional([int]) }] })

    def process(self, db, creator, review_id, filters):
        review = dbutils.Review.fromId(db, review_id)
        by_user = {}

        for filter in filters:
            if "user_ids" in filter:
                user_ids = set(filter["user_ids"])
            else:
                user_ids = set([])

            if "user_names" in filter:
                for user_name in filter["user_names"]:
                    user_ids.add(dbutils.User.fromName(db, user_name).id)

            if "paths" in filter:
                paths = set(reviewing.filters.sanitizePath(path) for path in filter["paths"])

                for path in paths:
                    try:
                        reviewing.filters.validatePattern(path)
                    except reviewing.filters.PatternError as error:
                        raise OperationFailure(
                            code="invalidpattern",
                            title="Invalid path pattern",
                            message="There are invalid wild-cards in the path: %s" % error.message)
            else:
                paths = set()

            if "file_ids" in filter:
                for file_id in filter["file_ids"]:
                    paths.add(dbutils.describe_file(file_id))

            for user_id in user_ids:
                reviewer_paths, watcher_paths = by_user.setdefault(user_id, (set(), set()))

                if filter["type"] == "reviewer":
                    reviewer_paths |= paths
                else:
                    watcher_paths |= paths

        pending_mails = []

        for user_id, (reviewer_paths, watcher_paths) in by_user.items():
            try:
                user = dbutils.User.fromId(db, user_id)
            except dbutils.InvalidUserId:
                raise OperationFailure(
                    code="invaliduserid",
                    title="Invalid user ID",
                    message="At least one of the specified user IDs was invalid.")
            pending_mails.extend(reviewing.utils.addReviewFilters(
                    db, creator, user, review, reviewer_paths, watcher_paths))

        review = dbutils.Review.fromId(db, review_id)
        review.incrementSerial(db)

        db.commit()

        mailutils.sendPendingMails(pending_mails)

        return OperationResult()

class RemoveReviewFilter(Operation):
    def __init__(self):
        Operation.__init__(self, { "filter_id": int })

    def process(self, db, user, filter_id):
        cursor = db.cursor()

        cursor.execute("SELECT review FROM reviewfilters WHERE id=%s", (filter_id,))
        review_id = cursor.fetchone()
        if not review_id:
            raise OperationFailure(
                code="nosuchfilter",
                title="No such filter!",
                message=("Maybe the filter has been deleted since you "
                         "loaded this page?"))

        cursor.execute("DELETE FROM reviewfilters WHERE id=%s", (filter_id,))

        review = dbutils.Review.fromId(db, review_id)
        review.incrementSerial(db)

        db.commit()

        return OperationResult()
