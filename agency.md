Agency is an open source platform for building, running, and monitoring multi step AI workflows, entirely on your own computer.

Most AI tools today are single assistants. You ask one model a question, and you get one answer. Real work is rarely that simple. A customer support desk has to read a ticket, decide what kind of problem it is, draft a reply, have a human approve it, and log the outcome. That is a chain of steps, and each step may need a different AI model to do it well.

Agency exists to run chains like that. You describe your workflow in a simple, human readable configuration file. Each step is a node on a map: an AI agent, a decision that splits the path, a step that fans the work out in parallel, a branch that joins paths back together, a tool that performs a specific job, or a human who must approve things before they move forward. When you start the workflow, Agency walks that map, feeds each step's result into the next, and manages everything in between. Each step can use its own model, so a fast cheap model handles easy tasks and a larger one handles the hard ones.

The part no other tool does: it manages your computer's graphics card memory automatically. Running several AI models at once can crash an ordinary machine. Agency keeps a constant eye on that memory, queues steps when space runs low, and moves idle models out of the way so the ones in use have room. You can set a hard limit, and Agency will never cross it.

Workflows are built to survive failure. Any step can retry itself a few times, and if it still fails, Agency hands the job to a backup agent instead of killing the whole run. Every step is saved as it completes, so if the machine crashes, the run picks up where it left off. And later, you can re run any part of a workflow without repeating work that already succeeded.

You can watch all of it happen. A built in terminal interface shows live progress for every step, and a companion Flutter desktop application renders the whole workflow as a living picture, with steps lighting up as they run, plus live metrics like time and token usage. When a workflow needs a human judgment, the application shows the prompt and waits for the answer before continuing.

Everything runs locally. The AI models run on your own hardware, a laptop or a workstation, so your data never leaves the machine and you pay nothing per word. Agency works with the popular local model servers, including llama.cpp, Ollama, and LM Studio.

Tool calls have support for running python or bash scripts, they run by default on a sandboxed docker container.

Under the hood, a few ideas hold the system together. A central event bus: every component announces what happened, and nothing talks to anything else directly, which keeps the system simple and easy to extend. A single orchestrator that alone decides when a step starts, finishes, or fails. A resource manager that owns every memory and model decision. And a write ahead journal that records every change before it happens, which is what makes crash recovery and replay possible.

The result is a platform where a business process is a file you can read, review, and trust, and where running it costs nothing per word and exposes nothing outside your own machine.
