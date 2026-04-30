# Setting up a Homebrew Tap for workforce-ai

This guide explains how to publish Workforce as a Homebrew formula via a third-party tap once the
package is live on PyPI.

---

## Prerequisites

- `workforce-ai` published on PyPI
- A GitHub account (the tap is just a public GitHub repository)
- Homebrew installed locally for testing

---

## 1. Create the tap repository

Homebrew taps are GitHub repositories named `homebrew-<tapname>`. Create a public repo:

```
Organization or user: workforce-ai   (or wyennie)
Repository name:      homebrew-workforce
```

URL: `https://github.com/workforce-ai/homebrew-workforce`

Users install the tap with:

```bash
brew tap workforce-ai/workforce
brew install workforce-ai
```

Homebrew automatically strips the `homebrew-` prefix, so `workforce-ai/homebrew-workforce`
becomes the tap `workforce-ai/workforce`.

---

## 2. Formula structure

A Homebrew tap repository looks like this:

```
homebrew-workforce/
├── Formula/
│   └── workforce-ai.rb
└── README.md
```

The formula file lives under `Formula/` and is named after the package (`workforce-ai.rb`).

---

## 3. Formula template

```ruby
class WorkforceAi < Formula
  include Language::Python::Virtualenv

  desc "Persistent roster of Claude specialists, dispatchable on tickets"
  homepage "https://github.com/wyennie/workforce"
  url "https://files.pythonhosted.org/packages/.../workforce_ai-0.1.0.tar.gz"
  sha256 "<sha256 of the sdist from PyPI>"
  license "MIT"

  depends_on "python@3.11"

  # Generate the resource stanzas with:
  #   poet -f workforce-ai
  # (pip install homebrew-pypi-poet)
  resource "claude-agent-sdk" do
    url "https://files.pythonhosted.org/packages/.../claude_agent_sdk-X.Y.Z.tar.gz"
    sha256 "<sha256>"
  end

  resource "typer" do
    url "https://files.pythonhosted.org/packages/.../typer-X.Y.Z.tar.gz"
    sha256 "<sha256>"
  end

  resource "pydantic" do
    url "https://files.pythonhosted.org/packages/.../pydantic-X.Y.Z.tar.gz"
    sha256 "<sha256>"
  end

  resource "rich" do
    url "https://files.pythonhosted.org/packages/.../rich-X.Y.Z.tar.gz"
    sha256 "<sha256>"
  end

  resource "tomli_w" do
    url "https://files.pythonhosted.org/packages/.../tomli_w-X.Y.Z.tar.gz"
    sha256 "<sha256>"
  end

  resource "prompt_toolkit" do
    url "https://files.pythonhosted.org/packages/.../prompt_toolkit-X.Y.Z.tar.gz"
    sha256 "<sha256>"
  end

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "workforce", shell_output("#{bin}/workforce --help")
  end
end
```

### Filling in the resource stanzas

The easiest way to populate all transitive dependencies is with the
[`homebrew-pypi-poet`](https://github.com/tdsmith/homebrew-pypi-poet) tool:

```bash
pip install homebrew-pypi-poet
poet -f workforce-ai
```

Paste the output into the formula above the `def install` block.

---

## 4. Bump the formula on each release

When a new version of `workforce-ai` is published to PyPI:

1. Download the new sdist and compute its SHA-256:

   ```bash
   curl -L https://pypi.org/packages/source/w/workforce-ai/workforce_ai-X.Y.Z.tar.gz \
     | sha256sum
   ```

2. Update `url` and `sha256` in `Formula/workforce-ai.rb`.

3. Re-run `poet -f workforce-ai==X.Y.Z` to refresh pinned resource versions.

4. Open a PR against `homebrew-workforce` — or push directly to `main` if you are the maintainer.

---

## 5. Automating formula bumps (optional)

You can automate step 4 with a GitHub Actions workflow in `homebrew-workforce`:

```yaml
# .github/workflows/bump.yml
name: bump formula

on:
  repository_dispatch:
    types: [new-release]

jobs:
  bump:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Install poet
        run: pip install homebrew-pypi-poet
      - name: Update formula
        env:
          VERSION: ${{ github.event.client_payload.version }}
        run: |
          poet -f "workforce-ai==${VERSION}" > Formula/workforce-ai.rb
      - name: Commit and push
        run: |
          git config user.email "bot@workforce.local"
          git config user.name "workforce-bot"
          git commit -am "chore: bump workforce-ai to ${VERSION}"
          git push
```

Then, in the main `workforce` release workflow, add a step that triggers this dispatch after
the PyPI upload succeeds.

---

## 6. Testing the formula locally

Before publishing, validate locally:

```bash
brew install --build-from-source ./Formula/workforce-ai.rb
brew test workforce-ai
brew audit --strict workforce-ai
```
