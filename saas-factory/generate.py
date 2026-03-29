#!/usr/bin/env python3
"""SaaS Factory — Project Generator."""

import argparse
import json
import os
from pathlib import Path

OUTPUT_DIR = Path("/data/projects")

STRUCTURE = [
    "src/app/(auth)/login/page.tsx",
    "src/app/(auth)/register/page.tsx",
    "src/app/(dashboard)/layout.tsx",
    "src/app/(dashboard)/page.tsx",
    "src/app/(dashboard)/settings/page.tsx",
    "src/app/(landing)/page.tsx",
    "src/app/api/health/route.ts",
    "src/app/layout.tsx",
    "src/components/ui/Button.tsx",
    "src/components/ui/Input.tsx",
    "src/components/ui/Card.tsx",
    "src/components/layout/Navbar.tsx",
    "src/components/layout/Sidebar.tsx",
    "src/components/layout/Footer.tsx",
    "src/lib/supabase/client.ts",
    "src/lib/supabase/server.ts",
    "src/lib/supabase/middleware.ts",
    "src/lib/utils.ts",
    "src/lib/validations.ts",
    "src/hooks/useAuth.ts",
    "src/hooks/useToast.ts",
    "src/types/index.ts",
    "src/i18n/tr.json",
    "src/i18n/en.json",
    "supabase/migrations/.gitkeep",
    "public/favicon.ico",
    ".env.example",
    ".gitignore",
    "tsconfig.json",
    "next.config.ts",
]


def make_page(name):
    return "export default function " + name + "() {\n  return <div>" + name + "</div>\n}\n"


def make_layout(name):
    return "export default function " + name + "({ children }: { children: React.ReactNode }) {\n  return <>{children}</>\n}\n"


def make_route():
    return 'import { NextResponse } from "next/server"\nexport async function GET() { return NextResponse.json({ ok: true }) }\n'


def generate(name: str):
    out = OUTPUT_DIR / name
    if out.exists():
        print(f"ERROR: {out} already exists")
        return

    print(f"Generating {name}...")
    out.mkdir(parents=True)

    # package.json
    pkg = {
        "name": name,
        "version": "0.1.0",
        "private": True,
        "scripts": {
            "dev": "next dev --turbopack",
            "build": "next build",
            "start": "next start",
            "lint": "next lint",
            "test": "vitest",
        },
        "dependencies": {
            "next": "^15.0",
            "react": "^19.0",
            "react-dom": "^19.0",
            "@supabase/supabase-js": "^2.45",
            "@supabase/ssr": "^0.5",
            "tailwindcss": "^4.0",
            "zod": "^3.23",
            "next-intl": "^4.0",
        },
        "devDependencies": {
            "typescript": "^5.7",
            "@types/react": "^19",
            "vitest": "^3.0",
            "eslint": "^9",
        },
    }
    (out / "package.json").write_text(json.dumps(pkg, indent=2))

    # Create files
    for filepath in STRUCTURE:
        p = out / filepath
        p.parent.mkdir(parents=True, exist_ok=True)
        stem = Path(filepath).stem
        component = stem.replace("page", "Page").replace("layout", "Layout")

        if filepath.endswith(".gitkeep"):
            p.touch()
        elif filepath == ".gitignore":
            p.write_text("node_modules/\n.next/\n.env\n.env.local\n")
        elif filepath == ".env.example":
            p.write_text("NEXT_PUBLIC_SUPABASE_URL=\nNEXT_PUBLIC_SUPABASE_ANON_KEY=\nSUPABASE_SERVICE_ROLE_KEY=\n")
        elif filepath == "tsconfig.json":
            p.write_text(json.dumps({"compilerOptions": {"target": "ES2017", "lib": ["dom", "dom.iterable", "esnext"], "jsx": "preserve", "module": "esnext", "moduleResolution": "bundler", "strict": True, "paths": {"@/*": ["./src/*"]}}, "include": ["**/*.ts", "**/*.tsx"], "exclude": ["node_modules"]}, indent=2))
        elif filepath == "next.config.ts":
            p.write_text('import type { NextConfig } from "next"\nconst config: NextConfig = {}\nexport default config\n')
        elif "page.tsx" in filepath:
            p.write_text(make_page(component))
        elif "layout.tsx" in filepath:
            p.write_text(make_layout(component))
        elif "route.ts" in filepath:
            p.write_text(make_route())
        elif filepath.endswith(".json") and "i18n" in filepath:
            lang = "tr" if "tr.json" in filepath else "en"
            data = {"common": {"app_name": name, "login": "Giris" if lang == "tr" else "Login", "register": "Kayit" if lang == "tr" else "Register"}}
            p.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        elif filepath.endswith((".ts", ".tsx")):
            p.write_text("// " + filepath + "\nexport {}\n")

    # Git init
    os.system(f'cd {out} && git init -q && git add -A && git commit -q -m "init: {name} from SaaS Factory"')

    file_count = sum(1 for _ in out.rglob("*") if _.is_file())
    print(f"  {file_count} files created in {out}")
    print(f"  Next: cd {out} && npm install && npm run dev")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    args = parser.parse_args()
    generate(args.name)
