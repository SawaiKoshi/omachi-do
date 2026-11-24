---
name: agent-team
description: AIチームの統括役。GitHub Issuesの agent:ready タスクを拾い、タスクごとのworktreeと子スレッド（Claudeのプラン・実装、Codexのレビュー）を起動して進める。ユーザーにはプラン承認・指摘の取捨・マージ判断だけを求める。「/agent-team」「タスクを回して」「チームで進めて」と言われたときに使う。
---

# agent-team（Claude Code 用の入口）

本体は `.agents/skills/agent-team/SKILL.md` です（Codex と共通）。
いますぐそのファイルを読み、書かれている手順に従ってください。`references/` などの参照先も、本体のフォルダーからの相対パスです。
