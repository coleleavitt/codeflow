pub fn leaf() -> u32 { 1 }
pub fn middle() -> u32 { leaf() + leaf() }
pub fn top() -> u32 { middle() }
pub struct S;
impl S { pub fn method(&self) -> u32 { leaf() } }
