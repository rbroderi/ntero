use std::env;
use std::fs;
use std::path::{Path, PathBuf};

fn copy_file(source: &str, destination: &Path) {
    if let Some(parent) = destination.parent() {
        fs::create_dir_all(parent).expect("failed to create staged resource directory");
    }
    fs::copy(source, destination).expect("failed to stage package resource");
    println!("cargo:rerun-if-changed={source}");
}

fn main() {
    let output = PathBuf::from(env::var_os("OUT_DIR").expect("OUT_DIR is required"));
    copy_file("pyproject.toml", &output.join("pyproject.toml"));
    copy_file(
        "ThirdPartyLicenses/squish-LICENSE.txt",
        &output.join("ThirdPartyLicenses/squish-LICENSE.txt"),
    );
}
