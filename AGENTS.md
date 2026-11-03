# Contributor Rules for Autonomous Agents

## Workflow and Source Maintenance

- **Maintained Source First**: All code, configuration, and asset changes must be authored and committed in the maintained repository. Never apply ad-hoc or uncommitted patches directly to the live server or phone filesystems.
- **Controlled Deployment**: Deployments to the live environment must execute strictly through reviewed release mechanisms that validate complete manifest hashes prior to live activation.
- **Actual-Page Browser Checks**: Frontend and UI acceptance testing must exercise the actual deployed role templates (`web/admin.html`, `web/family.html`, `web/login.html`) and routed user interactions, rather than relying solely on embedded fallback fixtures or parser passes.
- **Visual Design Standard**: For any task modifying, designing, restyling, or reviewing a visual interface, agents must read and strictly apply the visual design principles in `/home/astro/.agents/skills/unslop/SKILL.md`. Existing appearance and behavior must be preserved unless explicit design refinement is requested.
- **Operational Boundaries**: Respect runtime secrets, private certificates, user data, and disabled-service invariants. No live credentials or signing keys may ever be committed to repository source.
