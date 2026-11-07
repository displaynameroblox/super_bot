# Publishing v2.1.0.alpha.1

1. Review and commit the intended release changes. Package version is `2.1.0a1`;
   the public Git tag is `v2.1.0.alpha.1`.
2. Run `python -m pytest Ai -q` and require Linux/macOS CI to pass.
3. Provision `Ai/release-public.pem` and the matching RSA private key as the
   GitHub Actions secret `RELEASE_SIGNING_KEY`, following
   [server administration](../server-administration.md). Never commit the private key.
   The release workflow deliberately fails if signing is not configured.
4. Install build tooling with `python -m pip install build`, then run
   `python -m build`. Check the wheel and source archive contain no private data.
5. Commit the reviewed files, then publish the tag deliberately:

   ```sh
   git tag v2.1.0.alpha.1
   git push origin v2.1.0.alpha.1
   ```

6. Verify the workflow publishes a **prerelease** with the wheel, source archive,
   signed server archive, manifest and signatures. Smoke-test installation in a
   fresh environment and the documented server/client flow on Linux and macOS.

Creating these files does not publish a release. Do not reuse the tag for a
replacement build; increment the alpha number for subsequent releases. Update
both package metadata and the workflow's release-notes path for the next release.
