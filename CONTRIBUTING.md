# Contributing to Trinity

Thank you for your interest in contributing to Trinity! This document provides guidelines for contributing to the project.

## License

Trinity is licensed under the [Apache License 2.0](LICENSE). Unless you explicitly state otherwise, any contribution intentionally submitted for inclusion in Trinity by you shall be licensed under the Apache License 2.0, without any additional terms or conditions (per Section 5 of the license).

## Code of Conduct

- Be respectful and inclusive
- Focus on constructive feedback
- Help others learn and grow

## How to Contribute

> **Where things are tracked.** Trinity is open-core. The public issue tracker
> ([abilityai/trinity](https://github.com/abilityai/trinity/issues)) is for
> **bugs and core maintenance**. Feature ideas and roadmap discussion happen in
> [Discussions](https://github.com/abilityai/trinity/discussions) — maintainers
> triage accepted proposals into the product roadmap.

### Reporting Bugs

1. Check if the issue already exists in [GitHub Issues](https://github.com/abilityai/trinity/issues)
2. If not, create a new issue with:
   - Clear, descriptive title
   - Steps to reproduce
   - Expected vs actual behavior
   - Environment details (OS, Docker version, etc.)
   - Relevant logs or screenshots

### Suggesting Features

Feature ideas go in **Discussions**, not Issues — the public tracker stays focused on bugs, and maintainers curate the roadmap from accepted proposals.

1. Open a [Discussion](https://github.com/abilityai/trinity/discussions) describing the use case and the problem you're solving
2. Propose a solution (optional) and be open to alternatives
3. Accepted ideas are picked up by maintainers and tracked on the roadmap

### Pull Requests

The project follows a 4-stage SDLC: Todo → In Progress → In Dev → Done, tracked via GitHub Issues labels (`status-in-progress`, `status-in-dev`).

1. **Fork and clone** the repository
2. **Find or create an issue** — every PR must link to an issue
3. **Create a feature branch** from `dev`:
   ```bash
   git checkout dev && git pull origin dev
   git checkout -b feature/<issue-number>-your-feature-name
   ```
4. **Make your changes** following our coding standards
5. **Test your changes** locally
6. **Commit with clear messages**:
   ```bash
   git commit -m "feat: Add support for custom metrics"
   ```
7. **Push and create a PR** against `dev` — include `Fixes #N` in the description. `main` is reserved for release cuts.

### Commit Message Format

We use conventional commits:

- `feat:` New feature
- `fix:` Bug fix
- `docs:` Documentation only
- `style:` Formatting, no code change
- `refactor:` Code change that neither fixes nor adds
- `test:` Adding tests
- `chore:` Maintenance tasks

Examples:
```
feat: Add agent custom metrics API
fix: Correct context percentage calculation
docs: Update deployment guide for production
```

## Development Setup

### Prerequisites

- Docker and Docker Compose v2+
- Node.js 20+ (for frontend development)
- Python 3.11+ (for backend development)

### Local Development

```bash
# 1. Clone your fork
git clone https://github.com/YOUR_USERNAME/trinity.git
cd trinity

# 2. Configure environment
cp .env.example .env
# Edit .env with required values

# 3. Build base image
./scripts/deploy/build-base-image.sh

# 4. Start services
./scripts/deploy/start.sh

# 5. Access the platform
# Web UI: http://localhost
# API: http://localhost:8000/docs
```

### Running Tests

```bash
# Backend tests
cd tests
python -m pytest -v

# Frontend (if applicable)
cd src/frontend
npm run test
```

## Code Standards

### Python (Backend)

- Follow PEP 8
- Use type hints
- Document public functions with docstrings
- Keep functions focused and small

### TypeScript/JavaScript (Frontend, MCP Server)

- Use TypeScript for new code
- Follow existing code style
- Use meaningful variable names
- Add comments for complex logic

### Vue.js (Frontend)

- Use Composition API
- Follow Vue.js style guide
- Keep components focused
- Use Pinia for state management

## Project Structure

```
trinity/
├── src/
│   ├── backend/          # FastAPI - Python
│   ├── frontend/         # Vue.js 3 - TypeScript
│   ├── mcp-server/       # MCP Server - TypeScript
│   └── audit-logger/     # Audit Service - Python
├── docker/
│   ├── base-image/       # Agent base image
│   └── ...               # Service Dockerfiles
├── config/               # Configuration files
├── docs/                 # Documentation
└── tests/                # Test suite
```

## Areas for Contribution

### Good First Issues

Look for issues labeled `good first issue` - these are suitable for newcomers.

### Feature Development

- Agent template improvements
- UI/UX enhancements
- MCP tool additions
- Documentation improvements
- Test coverage

### Documentation

- Improve existing docs
- Add examples and tutorials
- Fix typos and clarify language
- Translate to other languages

## Questions?

- Open a [Discussion](https://github.com/abilityai/trinity/discussions) for questions
- Join our community (link coming soon)
- Email: [hello@ability.ai](mailto:hello@ability.ai)

## Recognition

Contributors will be recognized in:
- GitHub contributors list
- Release notes for significant contributions
- Special thanks section (for major features)

Thank you for contributing to Trinity!
