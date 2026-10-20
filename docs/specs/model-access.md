# Spec: model access

## Decision

Model access is **set up by the owner** (decision D9). The factory needs only a working OMP login in its OMP
home; which provider and plan is the owner's choice.

## Moving a login onto the factory (the server has no screen)

OMP supports moving stored credentials between machines:

1. The owner signs in with OMP on his own device (`omp login <provider>`).
2. `omp auth export` writes the stored credentials to a file.
3. The file is imported once into the factory's OMP home (`omp auth import`) and kept as a secret.
4. The exported file is deleted from the device.

When a login expires, the supervisor sends the owner a direct message and pauses work that needs the model.

## Usage limits

The supervisor reads `omp usage`, paces work, pauses before a limit and resumes after the reset, telling the owner in
the task topics. Owner priorities run first.
