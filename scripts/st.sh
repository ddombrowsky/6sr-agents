#!/bin/sh
#
# git status of every repo in /opt, one after the other.
#
# Each directory is visited in a SUBSHELL. The loop used to `cd $f ... cd ..` in place,
# so a directory that was missing (or a `cd` that failed for any other reason) left the
# following iterations running one level up from where they thought they were, walking
# out of /opt entirely. create.sh git-inits all of these; a "not a git repository" here
# means a volume that was built before it did, or by hand.

for f in master_agent template_repo* tools ; do
    [ -d "$f" ] || continue
    (
        cd "$f" || exit 1
        pwd
        git status
    )
done
