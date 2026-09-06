# Architecture

## Current boundary

The current application is a small Django service with three domain packages:

```text
analytics_platform
├── catalog
│   └── Workspace and Project
├── event_catalog
│   └── EventDefinition and event-envelope contract
└── common
    └── Shared abstract model fields
```

`catalog` owns the data boundaries. `event_catalog` documents known events inside a project. `common` contains code that is genuinely shared by more than one domain.

HTTP handling remains thin: URLs route to views, views validate through serializers, and Django models enforce persistent constraints. A separate service layer should be introduced only when business logic no longer fits cleanly in this flow.

## Deployment boundary

Testing, staging, and production are independent deployments. Each has its own application, database, workspace and project records, and credentials. Product deployments select the correct analytics destination through environment-specific configuration.

Events from all products in one environment share that environment's local project. This permits cross-product analysis without combining data from different deployment environments.

## Planned domains

Add these packages only when their behavior is implemented:

```text
ingestion/   # Credentials, capture, validation and deduplication
events/      # Event persistence and project-scoped querying
semantics/   # Metrics, funnels and governed business definitions
mcp/         # Thin AI-facing tools over project-scoped query services
```

The event envelope currently lives in `event_catalog/contracts.py`. Ownership should move to `ingestion` when that package is introduced.

## Required future rule

Every stored event and analytics query must be scoped to a project. MCP tools must not query tenant data without an explicit project context.
