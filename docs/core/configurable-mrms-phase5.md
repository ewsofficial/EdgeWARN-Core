# Configurable MRMS: staged readiness and replay safety

Detection inputs release first. Mandatory Core integration is separate from
bounded optional MRMS acquisition. The final CTAM snapshot preserves detection,
history, and integration selections while adding only aligned successful optional
inputs. Late arrivals cannot revise that immutable snapshot.

Core consumes exact product/family/role selections. Previous history is strictly
earlier than the current observation, and missing current data never selects
an arbitrary latest file. Scientific missing-data behavior remains fail-closed
for required features.

Cross-process input leases protect selection, worker pins, and retention. Cleanup
yields to active references and the rediscovery window. Historical processing
uses an isolated runtime root. Current implementation details and resource
settings are in [ingestion](ingestion.md) and [configuration](configuration.md).
