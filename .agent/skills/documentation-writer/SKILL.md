---
name: documentation-writer
description: Generate and maintain project documentation, API docs, and code comments.
---

# Documentation Writer Skill

This skill helps create and maintain comprehensive documentation.

## Capabilities

| Capability | Description |
|------------|-------------|
| API Documentation | Generate endpoint documentation |
| Code Comments | Add meaningful inline comments |
| README Generation | Create/update README files |
| Changelog Updates | Document version changes |
| JSDoc/Docstrings | Generate function documentation |

## Documentation Standards

### Function Documentation

#### Python (Docstrings)
```python
def process_data(data: list, threshold: float = 0.5) -> dict:
    """
    Process input data and return filtered results.
    
    Args:
        data: List of data points to process
        threshold: Minimum value to include (default: 0.5)
    
    Returns:
        Dictionary containing processed results with keys:
        - 'filtered': List of values above threshold
        - 'count': Number of filtered items
    
    Raises:
        ValueError: If data is empty
        TypeError: If data contains non-numeric values
    
    Example:
        >>> process_data([0.1, 0.7, 0.3], 0.5)
        {'filtered': [0.7], 'count': 1}
    """
```

#### JavaScript (JSDoc)
```javascript
/**
 * Process input data and return filtered results.
 * 
 * @param {Array<number>} data - List of data points to process
 * @param {number} [threshold=0.5] - Minimum value to include
 * @returns {Object} Processed results
 * @returns {Array<number>} return.filtered - Values above threshold
 * @returns {number} return.count - Number of filtered items
 * @throws {Error} If data is empty
 * 
 * @example
 * processData([0.1, 0.7, 0.3], 0.5)
 * // Returns: { filtered: [0.7], count: 1 }
 */
function processData(data, threshold = 0.5) {
```

## README Template

```markdown
# Project Name

Brief description of what this project does.

## Installation

\`\`\`bash
npm install
# or
pip install -r requirements.txt
\`\`\`

## Usage

\`\`\`bash
npm start
# or
python run.py
\`\`\`

## Configuration

| Variable | Description | Default |
|----------|-------------|---------|
| `PORT` | Server port | 3000 |

## API Reference

### `GET /api/endpoint`

Description of endpoint.

**Parameters:**
- `param` (string, required): Description

**Response:**
\`\`\`json
{ "data": "example" }
\`\`\`

## Contributing

See [CONTRIBUTING.md](../../../CONTRIBUTING.md)

## License

[License Name]
```

## Changelog Format

```markdown
# Changelog

## [1.2.0] - 2026-01-20

### Added
- New feature description

### Changed
- Updated feature description

### Fixed
- Bug fix description

### Removed
- Removed feature description
```
