// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "vox-vmic",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "vox-vmicctl", targets: ["vmicctl"]),
    ],
    targets: [
        .target(
            name: "VMicSharedC",
            path: "shared",
            publicHeadersPath: "include"
        ),
        .executableTarget(
            name: "vmicctl",
            dependencies: ["VMicSharedC"],
            path: "Sources/vmicctl",
            linkerSettings: [
                .linkedFramework("AVFoundation"),
            ]
        ),
    ]
)
