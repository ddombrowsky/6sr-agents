#!/bin/sh

for f in master_agent template_repo* tools ; do
    cd $f
    pwd
    git status
    cd ..
done
