# phase_a 附加资源说明

阶段 A 不需要额外的 Python 包；此目录用于放置构建期快照的附加资源（当前为空占位）。
业务代码（engine/、server/、templates/、static/）按设计方案在**阶段 C** 才接入，
且必须由 Gradle 按白名单生成快照，不手工镜像、不提交副本。
