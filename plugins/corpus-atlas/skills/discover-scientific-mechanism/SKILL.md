---
name: discover-scientific-mechanism
description: Use Corpus Atlas to run hypothesis-driven scientific discovery rather than property prediction. Build a frontier map, formulate competing mechanistic hypotheses, choose discriminating calculations or experiments, record observations, falsify weak explanations, and iterate toward a prospective discovery. Especially useful for computational chemistry, catalysis, materials science, molecular simulation, DFT, ML potentials, reaction networks, and projects that combine public evidence with new calculations or laboratory validation.
---

# Discover Scientific Mechanisms

Use Corpus Atlas as an evidence-governed scientific discovery workspace. The objective is not to train a predictor and stop. The objective is to produce a falsifiable new scientific claim and identify the cheapest observations that could kill it.

## 1. Define The Discovery Target

Start with a scientific unknown, anomaly, or unresolved competition between explanations.

Write the investigation question so that a new observation can change the scientific conclusion. Good examples include:

- Which catalyst state exists under operating conditions but is absent from the nominal static model?
- Which reaction channel becomes accessible only after coverage-induced restructuring?
- Which competing mechanism explains a measured regime change, and what observation distinguishes them?

Avoid questions whose endpoint is merely model accuracy, property prediction, or parameter optimization.

Initialize a science workspace:

```bash
corpus-atlas init WORKSPACE \
  --title "Mechanistic discovery" \
  --question "Which observation would distinguish the competing mechanisms?" \
  --domain science
```

## 2. Build A Capability Frontier, Not A Topic Bibliography

Map the field by what each line of work can actually do. For atomistic catalysis, useful capability lanes include:

1. static DFT energetics and predefined reaction pathways;
2. transition-state search and reaction-network enumeration;
3. universal or reactive ML interatomic potentials;
4. finite-temperature dynamics, coverage effects, and surface reconstruction;
5. microkinetic connection from atomistic states to observables;
6. autonomous or hypothesis-driven AI systems;
7. prospective experimental or operando validation.

For each anchor paper, atomize claims about capability and failure boundary. A method paper that accelerates NEB is evidence for faster transition-state search, not evidence that it autonomously discovers the correct mechanism. A foundation potential with low average error is not evidence that every newly visited reactive region is trustworthy.

Search concentrically from anchor papers using backward references, forward citations, recent windows, neighboring terminology, and searches designed to falsify the emerging novelty claim.

## 3. Separate Observations, Interpretations, And Hypotheses

Use one atomic proposition per claim:

```bash
corpus-atlas claim add WORKSPACE \
  --text "Reactive-coverage trajectories repeatedly visit a reconstructed surface basin" \
  --kind observed

corpus-atlas claim add WORKSPACE \
  --text "The reconstructed basin is the catalytically relevant working state" \
  --kind interpreted

corpus-atlas claim add WORKSPACE \
  --text "A condition-induced active-state ensemble controls selectivity" \
  --kind hypothesis
```

Never promote an ML-generated configuration directly to an observed physical fact. The observation is that a model trajectory visited a state; DFT or experiment must adjudicate whether the state is physically credible.

## 4. Maintain Competing Explanations Explicitly

Do not generate one favorite hypothesis and collect confirmations. Maintain at least one serious alternative and, when possible, a null explanation.

```bash
corpus-atlas claim relate WORKSPACE \
  --from H_DYNAMIC --to H_STATIC \
  --relation competes-with \
  --rationale "They assign the selectivity change to different catalyst states"
```

Useful claim relations are `competes-with`, `depends-on`, `predicts`, `explains`, `refines`, `supports`, and `contradicts`.

A claim-to-claim relation is reasoning structure, not source evidence. Keep documentary or experimental evidence in ordinary source-to-claim links.

## 5. Turn Uncertainty Into Discriminating Probes

A probe is a planned observation chosen because different hypotheses predict different outcomes. It can be a literature check, DFT calculation, NEB calculation, finite-temperature MLIP/DFT dynamics, microkinetic perturbation, spectroscopy measurement, isotope experiment, pressure jump, or laboratory catalyst test.

```bash
corpus-atlas probe add WORKSPACE \
  --title "Finite-temperature active-state search" \
  --target-claim H_DYNAMIC \
  --modality MLIP-MD \
  --question "Does a recurrent reconstructed state appear only under reactive coverage?" \
  --if-true "A recurrent state basin appears and survives DFT re-evaluation" \
  --if-false "Trajectories remain in the static-state basin" \
  --cost medium \
  --priority 0.9
```

Prioritize probes by expected discrimination per unit cost, not by how impressive the calculation looks. The preferred next step is often the cheapest decisive observation.

Use `corpus-atlas probe list WORKSPACE --status planned` to inspect the queue.

## 6. Use AI And ML Potentials As Proposal Engines

For atomistic discovery, separate exploration from adjudication:

**Exploration layer**

- literature and dataset search;
- structure and state generation;
- universal or locally tuned MLIP relaxation;
- long finite-temperature trajectories;
- candidate reaction-event detection;
- reaction graph expansion;
- clustering of recurrent states;
- generation of competing mechanistic explanations.

**Adjudication layer**

- targeted DFT energies and forces;
- DFT relaxation of candidate states;
- DFT NEB or transition-state refinement;
- uncertainty and out-of-distribution checks;
- kinetic consistency;
- experimentally observable signatures;
- prospective laboratory validation.

Do not define novelty as improved prediction error. Define novelty as a new state, mechanism, coupling, regime boundary, or experimentally testable explanation that was not encoded in the initial hypothesis space.

## 7. Record Results Without Automatic Truth Promotion

Create the observation first, then attach it to the probe result:

```bash
OBS=$(corpus-atlas claim add WORKSPACE \
  --text "DFT relaxation preserves the reconstructed motif and lowers the relevant barrier" \
  --kind observed)

corpus-atlas probe result WORKSPACE \
  --probe PROBE_ID \
  --observation-claim "$OBS" \
  --verdict supports \
  --summary "Supports the dynamic-state hypothesis but does not yet establish operando population"
```

A probe result does not automatically mark the target hypothesis as true. Update hypothesis status only after considering all supporting, contradicting, and qualifying evidence.

## 8. Require Prospective Predictions

Before the decisive experiment is run, freeze at least one prediction that differentiates the leading hypotheses. Prefer qualitative signatures that are hard to fit after the fact, such as:

- appearance or disappearance of a specific operando spectral feature;
- a pressure, coverage, or temperature regime boundary;
- isotope-dependent pathway switching;
- a non-monotonic activity/selectivity trend;
- dependence on a deliberately perturbed facet, interface, defect, or promoter concentration.

Retrospective agreement is useful but weaker than prospective discrimination.

## 9. Run A Novelty Audit Before Calling It A Discovery

When a candidate mechanism emerges, search for the phenomenon itself, not only the method keywords. Check synonyms, neighboring catalyst systems, older surface-science terminology, supplementary materials, cited datasets, and recent preprints.

Classify the result as one of:

- rediscovery of known behavior;
- known phenomenon in a new catalyst or regime;
- new mechanistic explanation of known data;
- new state or reaction channel;
- prospective prediction subsequently validated.

The last two are the strongest discovery targets.

## 10. Stop, Branch, Or Escalate Explicitly

Stop a hypothesis branch when a decisive probe contradicts a required premise, when repeated searches reach conceptual saturation, or when the remaining uncertainty cannot change the next action.

Escalate from public-data exploration to new DFT only when the calculation can distinguish surviving hypotheses. Escalate from DFT to experiment only when a prospective observable has been derived.

The final report must state:

- the current frontier map;
- the strongest surviving hypotheses and alternatives;
- what AI actually discovered versus reproduced;
- the decisive supporting and contradicting observations;
- the cheapest next probe;
- what result would falsify the preferred explanation;
- whether the claim is ready for prospective experimental validation.
