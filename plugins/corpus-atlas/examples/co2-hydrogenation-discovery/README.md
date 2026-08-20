# CO2 Hydrogenation Mechanistic Discovery Atlas

This example turns Corpus Atlas into an evidence-governed scientific discovery workspace for CO2 hydrogenation. The scientific endpoint is **a new, falsifiable mechanistic discovery**, not a better property predictor or a force field with a lower validation error.

## Discovery anchor

> **Can an evidence-governed AI system jointly discover a condition-induced catalytic working state and an associated reaction channel that are absent from the initial static model, then prospectively predict an observation that discriminates this mechanism from serious alternatives?**

The project therefore treats the catalyst state and the reaction network as coupled unknowns:

```text
operating conditions C = (T, P, gas composition, coverage, composition)
        |
        v
working-state ensemble S(C)  <---->  reaction graph R(S)
        |                              |
        +----------> kinetics K <-----+
                         |
                         v
              observable signatures O
                         |
                         v
                  DFT / experiment
                         |
                         +----> update or kill hypotheses
```

A potential or foundation model is an **exploration engine** inside this loop. It is not the scientific claim.

## Why this is the frontier rather than another MLIP project

Several formerly open subproblems are now occupied:

| Capability lane | 2025-2026 anchor | What is already demonstrated | What should not be claimed as our novelty |
| --- | --- | --- | --- |
| Reactive ML potentials | Yang et al., *Nature Catalysis* 2025, DOI: 10.1038/s41929-025-01398-3 | General reactive ML potentials can sample surface reconstruction, coverage effects and reaction chemistry | Merely building a reactive potential |
| MLIP reaction-network acceleration | Hou et al., *ACS Catalysis* 2026, DOI: 10.1021/acscatal.5c08361 | Universal MLIP + local fine-tuning + active learning accelerates CuZn methanol reaction-network exploration | MLIP fine-tuning or faster CRN exploration alone |
| MLMD reconstruction + kinetics | Rajaelo et al., *ACS Catalysis* 2026, DOI: 10.1021/acscatal.5c09279 | MLMD detects H-induced Cu(100) reconstruction and feeds the resulting state into microkinetics | Merely showing that a surface can reconstruct |
| Operando Cu/ZnO dynamics | Boniface et al., *Nature Catalysis* 2026, DOI: 10.1038/s41929-026-01514-x | ZnOx overlayers and CuZn surface alloys change reversibly with temperature and gas chemical potential | Generic claims that Cu/ZnO has dynamic active states |
| Dynamic oxide defects | Becker et al., *Nature Communications* 2026, DOI: 10.1038/s41467-026-72876-w | Dopants control oxygen-vacancy formation/healing dynamics in In2O3 and therefore activity/stability | Generic claims that oxygen-vacancy dynamics matter |
| General AI hypothesis generation | Gottweis et al., *Nature* 2026, DOI: 10.1038/s41586-026-10644-y | AI can generate, critique and refine experimentally testable scientific hypotheses | “An LLM generated hypotheses” by itself |
| Autonomous catalysis | Orouji et al., *Nature Catalysis* 2025, DOI: 10.1038/s41929-025-01430-6 | Closed-loop AI/robot catalysis and human oversight are established as a frontier | Generic autonomous-lab framing |

The remaining high-value gap is the **joint, falsifiable discovery problem**:

1. the relevant catalyst working state is not fixed in advance;
2. the relevant reaction graph is not fixed in advance;
3. AI must search both spaces together;
4. AI must maintain serious competing explanations rather than optimize one preferred story;
5. new DFT and experiments are chosen because they discriminate hypotheses;
6. at least one observable prediction is frozen before the decisive experiment.

## Two-track strategy

### Track A — public-data benchmark and rediscovery

Use a crowded, data-rich system such as Cu/CuZn as a **calibration target**, not as the main novelty claim.

The AI should attempt to recover without being directly told:

- condition-dependent surface reconstruction;
- coverage-sensitive states;
- known Cu/ZnOx or CuZn motifs where public evidence permits;
- the major competing CO2 hydrogenation branches;
- known sensitivity of kinetics to the working-state model.

Passing this track demonstrates that the discovery engine can recover established non-static chemistry and that its novelty audit prevents rediscovery from being mislabeled as discovery.

### Track B — prospective discovery on the collaborator's catalyst

Choose the final catalyst only after a target-gate review. Prefer a system with:

- plausible restructuring, redox, vacancy, interfacial, coverage or promoter dynamics;
- experimentally accessible synthesis and characterization;
- enough public structural/thermochemical information to seed exploration;
- important uncertainty about the actual working state or dominant pathway;
- a route to an observable that differentiates competing mechanisms;
- a tractable DFT reference method for the collaborator.

Avoid choosing a target simply because a large public dataset exists. Public-data richness is most useful for Track A; unresolved chemistry is more important for Track B.

## Corpus Atlas hypothesis graph

Start with explicit alternatives, for example:

```text
H0 STATIC
The nominal low-energy static surface and a conventional predefined pathway
are sufficient to explain activity/selectivity.

H1 DYNAMIC-STATE
Reactive conditions create a recurrent working-state ensemble that changes
which elementary steps are kinetically relevant.

H2 COVERAGE-ONLY
The apparent mechanism shift is caused by adsorbate interactions/coverage
without a qualitatively different catalyst state.

H3 COMPOSITION/DEFECT
A minority composition, defect, interface or redox state rather than global
surface reconstruction creates the dominant active ensemble.
```

Record them as `hypothesis` claims and connect them with `competes-with`, `depends-on`, `predicts`, and `explains` relations.

A preferred mechanism is not promoted because the AI likes it. It survives only if discriminating probes fail to kill it.

## What the AI actually does

### 1. Frontier cartography and novelty firewall

- Build the literature/citation corpus from anchor papers and adversarial searches.
- Atomize what each paper actually establishes.
- Separate method capability from chemical discovery.
- Maintain a list of already-known states, pathways, descriptors and operando signatures.
- Search a newly generated candidate against synonyms and neighboring catalyst literatures before calling it novel.

**Output:** a capability frontier plus an occupancy map of claims that are already taken.

### 2. Mechanistic hypothesis generation

The AI proposes competing explanations from gaps, contradictions, condition dependence and anomalies in the corpus. Every hypothesis must specify:

- candidate state or state ensemble;
- causal mechanism;
- reaction-network consequence;
- expected kinetic consequence;
- at least one differentiating observable;
- failure condition.

**Output:** a ranked but explicitly contestable hypothesis set.

### 3. Joint state-space and reaction-space exploration

Use public structures, datasets and foundation/reactive ML potentials to explore:

- facets and terminations;
- defects and vacancies;
- dopants/promoters and interfaces;
- adsorbate coverages and co-adsorbates;
- temperature- and pressure-relevant configurations;
- reconstruction and redox events;
- bond-breaking/forming events and previously omitted intermediates.

The AI clusters recurrent states, detects reaction events, expands a state-conditioned reaction graph and asks whether new states create new pathways or change barriers.

**Output:** candidate working-state basins and state-conditioned reaction subgraphs.

### 4. Active DFT adjudication

The AI does not request DFT uniformly. It asks for the calculation that maximizes discrimination between surviving explanations. Examples:

- single-point energy/force checks on OOD frames;
- relaxation of candidate reconstructed states;
- energy ranking across competing states at relevant coverage;
- NEB/TS refinement for a pathway that exists only in one state;
- short ab initio MD checks where the MLIP state is especially consequential.

**Output:** a small, information-rich DFT queue for the collaborator.

### 5. Mechanism-to-observable translation

For each surviving mechanism, derive prospective signatures such as:

- different apparent activation energies or reaction orders;
- pressure/temperature regime transitions;
- non-monotonic selectivity;
- isotope effects or isotope-switch transients;
- predicted operando DRIFTS/XAS/XPS/Raman signatures;
- perturbation response to facet, promoter, defect or support changes.

**Output:** experiments that distinguish mechanisms, not experiments that merely produce more data.

### 6. Prospective validation and update

Freeze the prediction before the experiment. Record the experimental result as an `observed` claim, attach it to the discriminating probe, and update the competing hypotheses.

A negative experiment is scientifically useful if it kills a mechanism branch.

## Public-data phase: what can be completed before new DFT or wet lab work

The public-only phase should deliver a **pre-discovery dossier**, not pretend that an unvalidated ML trajectory is a physical discovery.

Minimum deliverables:

1. current frontier map with occupied and open claims;
2. benchmark corpus for Cu/CuZn or another data-rich system;
3. successful rediscovery tests for at least two known non-static phenomena;
4. target-system public corpus and initial state/reaction graph;
5. 3-10 explicit competing mechanistic hypotheses;
6. candidate active states/pathways generated with public models/data;
7. novelty audit for each high-value candidate;
8. uncertainty/OOD assessment;
9. prioritized DFT queue, ideally tens to low hundreds of high-information structures rather than indiscriminate thousands;
10. 2-5 prospective experimental signatures that would differentiate the top hypotheses.

At that point the collaborator receives a constrained scientific problem: **which calculations and measurements can establish or kill each proposed mechanism?**

## Example Corpus Atlas session

```bash
WORKSPACE=investigations/co2-hydrogenation

corpus-atlas init "$WORKSPACE" \
  --title "CO2 hydrogenation working-state discovery" \
  --question "Which condition-induced catalyst state changes the dominant reaction network, and what observation distinguishes it from static and coverage-only explanations?" \
  --domain science

H0=$(corpus-atlas claim add "$WORKSPACE" \
  --text "The nominal static surface is sufficient to explain the observed selectivity" \
  --kind hypothesis)

H1=$(corpus-atlas claim add "$WORKSPACE" \
  --text "A condition-induced working-state ensemble opens or stabilizes a different kinetically relevant reaction channel" \
  --kind hypothesis)

corpus-atlas claim relate "$WORKSPACE" \
  --from "$H1" --to "$H0" --relation competes-with \
  --rationale "They assign the selectivity to different catalyst-state ensembles"

corpus-atlas probe add "$WORKSPACE" \
  --title "Reactive-condition state search" \
  --target-claim "$H1" \
  --modality MLIP-MD \
  --question "Does a recurrent non-static state emerge under reactive coverage and remain metastable after targeted DFT relaxation?" \
  --if-true "A recurrent state survives DFT and yields a distinct state-conditioned reaction subgraph" \
  --if-false "The sampled states collapse to the nominal surface or do not alter relevant chemistry" \
  --cost medium --priority 0.9
```

## Decision gates

### Gate 0 — novelty

If the candidate phenomenon is already established in the same catalyst and regime, label it a rediscovery and use it as a benchmark.

### Gate 1 — model credibility

Do not escalate an MLIP event unless targeted DFT confirms the consequential state/energy/force relationship within a predeclared tolerance appropriate to the mechanistic claim.

### Gate 2 — mechanistic consequence

A reconstructed or defect state is not enough. It must change a reaction channel, barrier hierarchy, coverage response, kinetic flux, stability pathway, or another scientifically meaningful quantity.

### Gate 3 — discriminating observable

Do not escalate to a large experimental campaign unless the preferred and competing mechanisms predict measurably different outcomes.

### Gate 4 — prospective validation

Freeze the prediction before observing the decisive measurement.

## Publication ladder

The scientific paper should be organized around **what was discovered**, while the AI workflow is the means of discovery.

- **Level 1:** public-data rediscovery + method validation. Useful systems paper or computational workflow result, but not the desired endpoint.
- **Level 2:** new state/pathway confirmed by targeted DFT, with kinetic consequence and strong novelty audit. Potential computational catalysis paper.
- **Level 3:** new mechanism with prospective experimental signature and collaborator validation. Strong catalysis/chemistry paper.
- **Level 4:** a generalizable scientific principle—such as state/network feedback governing selectivity across more than one catalyst family—plus prospective operando validation. This is the route toward the highest-impact catalysis venues.

The strongest manuscript claim is therefore not:

> We trained an accurate ML potential for CO2 hydrogenation.

It is closer to:

> An AI-guided, falsification-driven search discovered a condition-induced working-state ensemble that reorganizes the accessible reaction network; a pre-registered mechanistic signature was subsequently observed experimentally.

## Initial references

- Yang, C. et al. General reactive element-based machine learning potentials for heterogeneous catalysis. *Nature Catalysis* 8, 891-904 (2025). DOI: 10.1038/s41929-025-01398-3.
- Hou, P. et al. Accelerating Catalytic Reaction Network Exploration via Local Fine-tuning with Universal Machine Learning Interatomic Potentials. *ACS Catalysis* 16, 6443-6452 (2026). DOI: 10.1021/acscatal.5c08361.
- Rajaelo, W. O. N. F. et al. Bridging the Pressure Gap in Hydrogenation of CO2 to Formate on Cu(100) by Machine Learning Molecular Dynamics and First-Principles Microkinetic Modeling. *ACS Catalysis* 16, 5068-5079 (2026). DOI: 10.1021/acscatal.5c09279.
- Boniface, M. et al. Dynamics of a Cu/ZnO/Al2O3 catalyst revealed by operando transmission electron microscopy during CO2 hydrogenation. *Nature Catalysis* 9, 404-413 (2026). DOI: 10.1038/s41929-026-01514-x.
- Becker, M. et al. Dopant-controlled oxygen vacancy dynamics define CO2-to-methanol catalysis on In2O3. *Nature Communications* 17, 6435 (2026). DOI: 10.1038/s41467-026-72876-w.
- Gottweis, J. et al. Accelerating scientific discovery with Co-Scientist. *Nature* 655, 487-496 (2026). DOI: 10.1038/s41586-026-10644-y.
- Orouji, N. et al. Autonomous catalysis research with human-AI-robot collaboration. *Nature Catalysis* 8, 1135-1145 (2025). DOI: 10.1038/s41929-025-01430-6.
