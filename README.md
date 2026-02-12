# StarkAssistant

## ...

### Flowchart

```mermaid
flowchart TD
    E --> G[pending_delete]
    G --> H[sunset (soft delete)]
    G --> I[restore active]
```

### JSON Example

```json
{
    "key1": "value1",
    "key2": "value2"
}
```