# Data Model

## Relationship

```text
Workspace
└── Project
    └── EventDefinition
```

All current models use UUID primary keys and created/updated timestamps.

## Workspace

Represents an organization using the analytics platform. Its `key` is globally unique inside one deployed database.

Fields:

- `key`: stable machine-readable name;
- `name`: display name;
- `is_active`: disables use without deleting the record.

## Project

Represents a data and access boundary inside a workspace. It is not a source-code repository, deployment environment, or billed product.

Fields:

- `workspace`: owning workspace;
- `key`: machine-readable name, unique inside the workspace;
- `name`: display name;
- `is_active`: disables use without deleting the record.

All HappyFox products in the same analytics deployment initially share one local project. A standard event property identifies the business product.

## EventDefinition

Represents a known event name inside a project. It is documentation and discovery metadata, not a strict JSON Schema.

Fields:

- `project`: owning project;
- `name`: event name, unique inside the project;
- `description`: human explanation;
- `owner`: responsible team or person;
- `status`: `visible`, `verified`, or `hidden`.

An event definition does not declare required properties and does not reject unknown event properties.

## Not implemented

There are currently no stored event, ingestion credential, person, account profile, identity mapping, metric, or MCP models.
