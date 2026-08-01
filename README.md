An agent scaffolding system written by 6th Street Radio and David Dombrowsky. 

Main loop:
* Spawn a bunch of strategies.
* Do stuff for a while to make pretend money.
* Winners get cloned and adjusted using a master agent.  Losers are stopped.
* The Emperor agent watches over all things, revising things as needed.

Coded to talk to an ollama server running on the local host, accessible via
the docker gateway.  The agents run as root in a docker container.

