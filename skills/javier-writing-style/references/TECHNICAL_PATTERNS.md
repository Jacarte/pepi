# Technical Patterns from Javier's Published Posts

This reference document shows proven patterns from Javier's actual blog posts.

## Pattern 1: Root Cause Analysis (BuildKit Post)

**Structure:**
1. Surface problem (slow CI builds)
2. Intuitive wrong answer (use `--cache-to/from` flags)
3. Diagnosis of why intuitive answer fails
4. Real culprit identified (`--mount=type=cache` persistence)
5. Industry solutions mentioned (depot.dev)
6. Why those solutions work

**Example opening:**
```
In most modern CI/CD setups, we package applications into container images. 
This happens inside ephemeral runners. This architecture scales beautifully 
but introduces one major bottleneck: loss of internal build cache.

At first glance, Docker's layer caching and GitHub/GitLab cache features 
seem like the solution. Yet, they still can't accelerate the internal 
BuildKit cache system that Docker relies on.

The real culprit: `--mount=type=cache`
```

This pattern works because:
- Reader understands the constraint (ephemeral runners)
- Intuitive solution is acknowledged but shown incomplete
- Root cause is isolated and explained
- Reader now understands the mechanics before seeing the solution

## Pattern 2: Problem-Solution-Results (ONNX Post)

**Opening with curiosity:**
```
My initial attempt focused on discovering a method to directly execute 
the h5 file. Unfortunately, this effort proved unsuccessful. Yet, during 
the process, I learned about ONNX.
```

**Shows the journey:**
- What was tried (h5 direct execution) — failed
- What was discovered (ONNX ecosystem) — led to solution
- Why this matters (ONNX + WebAssembly = browser inference)

**Results with precision:**
```
On my computer, it takes nearly 30ms to infer from a 2Mb file. 
Please note, this time not only includes the inference but also 
the conversion of the binary into a grayscale image.
```

This pattern works because:
- Readers see honest experimentation, not magical answers
- Each step builds naturally from the previous one
- Results are specific (30ms, 2Mb) with context

## Pattern 3: Complete Configuration Examples

From BuildKit post — full K8s deployment with 135 lines:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  labels:
    app: buildkit-agent
  name: chatlayer-buildkit-buildkit-agent
  namespace: buildx
# ... [full config] ...
```

**Why include complete configs:**
- Readers can copy-paste and run
- No ambiguity about "how much" to configure
- Shows real production constraints (resource limits, security context)
- Demonstrates thinking (why these specific settings?)

**Documentation pattern:**
```
#### Persistent storage

Third, we create a PersistentVolumeClaim to provide persistent storage 
for the BuildKit daemon. This ensures that the build cache and 
intermediate files are preserved across restarts.

Here we define the PersistentVolumeClaim:
[FULL YAML]

> Notice that a StatefulSet would be a better fit for this use case, 
  but for simplicity we used a Deployment with a single replica.
```

## Pattern 4: Quantified Results with Context

**Bad:** "Much faster builds"

**Good (from BuildKit post):**
```
For our Go services, the very first time we run the Dockerfile, 
we see an empty cache and the build takes around 2 minutes. 
However, on subsequent builds, the cache is reused and the size 
is about a few hundred megabytes, and the build time drops to 
just 12 seconds!

The image below shows the time comparison: blue for traditional 
docker build (no persistent cache), orange for docker buildx build 
with remote BuildKit daemon.

Across our Go services, the improvement is dramatic. For 6 out of 7 
services, build time dropped from approximately 2 minutes down to 
10–17 seconds — a reduction of ~90–95%.
```

**Elements:**
- Before metric (2 minutes)
- After metric (12 seconds)
- Percentage reduction (90-95%)
- Context (Go services, not all services equally)
- Visual (chart reference)
- Caveats (Node.js service behaved differently)

## Pattern 5: Trade-off Exploration

From BuildKit post — Node.js service performance inversion:

```
However, the Node.js service showed the opposite behavior: 
the build time increased by ~3 minutes. After investigation, 
the cause was the `--load` flag, which imports the built 
image back into the local Docker daemon after the build completes.

At the moment, we still rely on `--load` because downstream 
steps need access to the built image locally for validation. 
However, a cleaner approach is available: run the remote builder 
within the same network as the registry and switch to `--push`.

This pushes the image directly to the container registry 
without exporting layers back to the CI runner, eliminating 
the network transfer bottleneck entirely.
```

**Why this pattern works:**
- Acknowledges the limitation (Node.js slower)
- Explains the root cause (--load flag overhead)
- Provides rationale for current approach ("downstream steps need...")
- Suggests better future approach ("However, a cleaner approach...")

## Pattern 6: Blockquote Asides

**Type 1: Important caveat**
```
> This was added even before we set up the remote BuildKit daemon, 
  but it has no effect due to the ephemeral nature of CI runners.
```

**Type 2: Industry context**
```
> ONNX is an open format built to represent machine learning models. 
  ONNX defines a common set of operators and a common file format 
  to enable AI developers to use models with a variety of frameworks, 
  tools, runtimes, and compilers.
```

**Type 3: Methodology note**
```
> Disclaimer: The onnxruntime project provides tutorials aimed at 
  achieving this goal. Yet, note that in existing tutorials, 
  data preprocessing is not handled within the WebAssembly binary. 
  The key distinction in my approach lies in precisely encapsulating 
  all components within the same Wasm program.
```

**Type 4: Honest limitation**
```
> Notice this code is just an MVP. I am sure there exist better ways 
  to implement such code. To begin with, the `unwrap`ing is definitely 
  a bad practice.
```

These blockquotes serve as:
- Disclaimer (not foolproof, temporary, MVP)
- Education (what is ONNX)
- Comparison (how my approach differs)
- Humility (honest limitations)

## Pattern 7: Narrative Transitions

**From problem to solution:**
```
The solution is clear — we need persistent builders. We deploy a remote 
BuildKit daemon with persistent storage in our Kubernetes cluster, ensuring 
that cache data survives across builds and can be reused by all CI jobs.
```

**From explanation to implementation:**
```
Let's dissect the important parts.
```

**From code to results:**
```
### Results

For our Go services, we use the following cache mount in the Dockerfile...
The very first time we run the Dockerfile above, we see an empty cache...
```

**From results to discussion:**
```
## Discussion

I avoid drawing comparisons between this approach and others. I leave this 
task to you. In lieu of this, I will outline two specific points I have 
observed throughout the process.
```

## When to Use These Patterns

| Pattern | Use When |
|---------|----------|
| Root Cause Analysis | Solving infrastructure/system problem where intuitive answer isn't enough |
| Problem-Solution-Results | Exploring implementation journey (what was tried, what worked, why) |
| Complete Configs | Explaining setup/deployment where precision and reproducibility matter |
| Quantified Results | Demonstrating performance improvement or capability |
| Trade-off Exploration | Solution has limitations that depend on context |
| Blockquote Asides | Need to acknowledge limitation, provide context, or show methodology |
| Narrative Transitions | Guide reader through logical flow of reasoning |

---

## Anti-Pattern: The Sales Pitch

Javier's style is NOT:
- "We built an amazing tool that solves everything"
- "Here's the one right way to do this"
- "Our approach is superior to all alternatives"

Instead:
- "We had a specific problem. Here's why common approaches didn't work. Here's what we did. Here are the quantified results. Here are the limitations."

This builds trust because readers see honest tradeoffs, not marketing hype.
