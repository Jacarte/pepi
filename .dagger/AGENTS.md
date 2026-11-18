# Rules

- **Keep workflow logic in Dagger.** If a workflow requires more than one simple Bash command, add or extend a Dagger function to handle it. For example, to run the same evaluation multiple times, add a parameter for the iteration limit and implement the loop inside the Dagger function.

- **Invoke Dagger with a single, simple Bash command.** Keep loops, conditionals, filtering, and other orchestration logic inside Dagger. Do not use shell pipelines, command chaining, or other techniques to compress multiple steps into a Bash one-liner.
