#!/bin/sh
#

docker run -d --name agenttest --volume ./v:/opt ubuntu sleep infinity
