# Runtime V2 Calibration Results

- Overall: `passed`
- Samples: 20 measured + 3 warmups per route
- Python: 3.11.5

| Route | Success | p95 ms | Mean tokens | Mean model calls |
|---|---:|---:|---:|---:|
| legacy_standard | 100.00% | 1000.8427 | 128.0000 | 1.0000 |
| unified_standard | 100.00% | 1032.4471 | 128.0000 | 1.0000 |
| serial_equivalent_workload | 100.00% | 6034.6978 | 500.0000 | 1.0000 |
| ultra_read_fanout | 100.00% | 3838.9541 | 500.0000 | 5.0000 |

## Gate Evaluation

- Standard: `passed`; success +0.0000 pp, p95 +3.1578%, tokens +0.0000%, model calls +0.0000.
- Ultra: `passed`; success +0.0000 pp, wall-clock improvement 36.3853%.

This is a controlled Runtime overhead/concurrency calibration, not a claim about any external model provider.
