# Third-Party Notices

AxonLLM vendors a small set of browser libraries so the operator and chat
interfaces can run without a public CDN. Generated media is also committed so
the packaged demo works without build-time cloud access.

## Vendored Browser Libraries

The following files retain their upstream copyright and license headers:

| Component | Version | Files | License |
|---|---:|---|---|
| React | 18.3.1 | `src/gateway/*/static/vendor/react.production.min.js` | MIT |
| ReactDOM | 18.3.1 | `src/gateway/*/static/vendor/react-dom.production.min.js` | MIT |
| Babel Standalone | 7.24.7 | `src/gateway/admin/static/vendor/babel.min.js` | MIT |

React and ReactDOM are copyright Meta Platforms, Inc. and affiliates. Babel is
copyright the Babel contributors. Their source repositories contain the
corresponding source code and complete project notices.

These components are distributed under the MIT License:

> Permission is hereby granted, free of charge, to any person obtaining a copy
> of this software and associated documentation files (the "Software"), to deal
> in the Software without restriction, including without limitation the rights
> to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
> copies of the Software, and to permit persons to whom the Software is
> furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all
> copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
> IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
> FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
> AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
> LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
> OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
> SOFTWARE.

## Generated Project Media

The screenshots, diagrams, captions, demo video, and narration scripts in this
repository document AxonLLM itself. The checked-in narration audio is generated
from those project-authored scripts with Amazon Polly by
`scripts/build_narration_audio.sh` and the scripts under `scripts/demo/`. No
third-party source recording is embedded. The narration helper also supports
local macOS speech for uncommitted previews; those local-preview outputs are not
shipped.

Product and service names mentioned in the demonstrations remain the trademarks
of their respective owners.
